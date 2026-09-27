"""Tests for the Extended IEC 61131-2 export parser."""

from __future__ import annotations

from pathlib import Path

from motionworks_iec_mcp_server.model import Project
from motionworks_iec_mcp_server.parsers import extended_iec


def _parse(root: Path) -> Project:
    project = Project()
    return extended_iec.parse(root, project)


class TestDetection:
    def test_detects_an_export_folder(self, extended_export: Path):
        assert extended_iec.is_extended_export(extended_export)

    def test_rejects_an_empty_folder(self, tmp_path: Path):
        assert not extended_iec.is_extended_export(tmp_path)

    def test_rejects_a_file(self, extended_export: Path):
        assert not extended_iec.is_extended_export(extended_export / "Starter.ST")


class TestPous:
    def test_st_pou_is_parsed(self, extended_export: Path):
        project = _parse(extended_export)
        starter = project.pous["Starter"]
        assert starter.pou_type == "program"
        assert starter.language == "ST"
        assert "Startup logic" in starter.description
        assert starter.body.statements == 3

    def test_description_block_is_extracted(self, extended_export: Path):
        """Regression: a greedy key name swallowed its own body.

        ``(*@KEY@:DESCRIPTION*)`` must parse as name='DESCRIPTION' with the text
        between the two markers as the body — not as name='DESCRIPTION*'.
        """

        from motionworks_iec_mcp_server.util import extract_description, extract_key_block

        text = (
            "(*@KEY@:DESCRIPTION*)\n"
            "Startup logic for the demo axis.\n"
            "(*@KEY@:END_DESCRIPTION*)\n"
        )
        assert extract_description(text) == "Startup logic for the demo axis."
        assert extract_key_block(text, "END_DESCRIPTION").strip() == ""

    def test_st_body_is_plain_source(self, extended_export: Path):
        project = _parse(extended_export)
        code = project.pous["Starter"].body.code
        # The worksheet region, not the declaration block or the END_PROGRAM.
        assert "Axis1.AxisNum := UINT#1;" in code
        assert "IF TopCutter_Homed AND xPermit THEN" in code
        assert "END_PROGRAM" not in code
        assert "VAR_EXTERNAL" not in code

    def test_interface_scopes_and_comments(self, extended_export: Path):
        project = _parse(extended_export)
        starter = project.pous["Starter"]
        external = starter.vars["VAR_EXTERNAL"]
        assert {v.name for v in external} == {"Axis1", "TopCutter_Homed"}
        homed = next(v for v in external if v.name == "TopCutter_Homed")
        assert homed.type_name == "BOOL"
        assert homed.comment == "Top Cutter Axis is Homed"
        assert [v.name for v in starter.vars["VAR"]] == ["xPermit", "iState"]

    def test_language_is_taken_from_the_header_not_the_extension(
        self, extended_export: Path
    ):
        """``.GE`` holds both LD and FBD — only the header distinguishes them."""

        project = _parse(extended_export)
        assert project.pous["Wiring"].language == "LD"

    def test_ge_body_is_kept_as_raw_text_for_the_graphical_decoder(
        self, extended_export: Path
    ):
        project = _parse(extended_export)
        wiring = project.pous["Wiring"]
        assert "[GRA]" in wiring.body.code
        # Decoding happens in project.py so the .GE parser stays testable alone.
        assert wiring.body.nets == []


class TestGlobalVariables:
    def test_globals_with_addresses_and_comments(self, extended_export: Path):
        project = _parse(extended_export)
        by_name = {v.name: v for v in project.global_vars}
        assert by_name["PLC_SYS_TICK_CNT"].address == "%MD1.0"
        assert by_name["PLC_SYS_TICK_CNT"].type_name == "DINT"
        assert by_name["AXIS1_SI1_POT"].address == "%IX53376.0"

    def test_group_headings_become_comments(self, extended_export: Path):
        """The group heading is the only place the amplifier/node context lives."""

        project = _parse(extended_export)
        by_name = {v.name: v for v in project.global_vars}
        assert "[AXIS1" in by_name["AXIS1_BRK"].comment
        # The inline comment must survive alongside the heading.
        assert "Brake" in by_name["AXIS1_BRK"].comment

    def test_all_globals_use_global_scope(self, extended_export: Path):
        project = _parse(extended_export)
        assert {v.scope for v in project.global_vars} == {"VAR_GLOBAL"}


class TestTasks:
    def test_task_name_type_timing_and_programs(self, extended_export: Path):
        project = _parse(extended_export)
        task = project.tasks["FastTsk"]
        assert task.task_type == "CYCLIC"
        assert task.interval == "T#4ms"
        assert task.priority == 0
        assert task.watchdog == "4"
        assert task.programs == ["Starter"]

    def test_resource_supplies_processor_type(self, extended_export: Path):
        project = _parse(extended_export)
        assert project.processor_type == "MP3300iec"


class TestIoConfig:
    def test_io_points_are_parsed_with_group_and_driver(self, extended_export: Path):
        project = _parse(extended_export)
        by_name = {p.name: p for p in project.io_points}
        assert set(by_name) == {"IMO1", "IAX1"}
        imo1 = by_name["IMO1"]
        assert imo1.direction == "INPUT"
        assert imo1.task == "FastTsk"
        assert imo1.driver_name == "LIODrv"
        assert imo1.var_addr == "61440"
        assert imo1.group == "YEA Input Group <Controller I/O>"

    def test_servo_group_records_the_network_node(self, extended_export: Path):
        project = _parse(extended_export)
        iax1 = next(p for p in project.io_points if p.name == "IAX1")
        assert "SGD7S" in iax1.group
        assert "Network #1" in iax1.group


class TestTypes:
    def test_struct_type_is_parsed(self, extended_export: Path):
        project = _parse(extended_export)
        cam = project.types["CamStruct"]
        assert cam.kind == "struct"
        assert [m.name for m in cam.members] == ["PartLength", "KnifeDiameter", "Count"]
        assert cam.members[0].type_name == "LREAL"
        assert "Part length" in cam.members[0].comment
