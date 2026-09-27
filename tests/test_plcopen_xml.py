"""Tests for the PLCopen XML parser and the LD/FBD renderer.

Several of these guard structural traps that silently produce empty results: the
``<pous>`` element is nested inside ``<types>``, ``<configurations>`` inside
``<instances>``, and ``<task>``/``<globalVars>`` inside ``<resource>``.
"""

from __future__ import annotations

from pathlib import Path

from motionworks_iec_mcp_server import project as pj
from motionworks_iec_mcp_server.model import Project
from motionworks_iec_mcp_server.parsers import plcopen_xml


def _parse(path: Path) -> Project:
    """Parse the XML alone, without the cross-source post-processing."""

    project = Project()
    return plcopen_xml.parse(path, project)


def _load(path: Path) -> Project:
    """Load through the full pipeline, including language inference."""

    return pj.load(path, use_cache=False)


class TestDetection:
    def test_detects_a_plcopen_export(self, plcopen_file: Path):
        assert plcopen_xml.is_plcopen_xml(plcopen_file)

    def test_rejects_a_non_xml_file(self, tmp_path: Path):
        other = tmp_path / "notes.txt"
        other.write_text("hello", encoding="utf-8")
        assert not plcopen_xml.is_plcopen_xml(other)


class TestHeader:
    def test_product_and_project_name(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        assert project.product_name == "MotionWorks IEC 3 Pro"
        assert project.product_version == "3.7.5.1"
        assert project.name == "SampleProject"


class TestPous:
    def test_pous_are_found_despite_being_nested_in_types(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        assert sorted(project.pous) == ["Empty", "Starter", "Wiring"]

    def test_st_body_is_unwrapped_without_reindenting(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        code = project.pous["Starter"].body.code
        assert "xPermit := TRUE;" in code
        assert "<br" not in code
        assert "<p" not in code
        assert "&#9;" not in code
        # The tab in front of the nested assignment must survive.
        assert "\txPermit := FALSE;" in code

    def test_interface_variables_with_scope_and_comment(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        starter = project.pous["Starter"]
        external = starter.vars["VAR_EXTERNAL"]
        assert external[0].name == "TopCutter_Homed"
        assert external[0].comment == "Top Cutter Axis is Homed"
        assert starter.vars["VAR"][0].name == "xPermit"

    def test_empty_body_is_reported_rather_than_invented(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        empty = project.pous["Empty"]
        assert (empty.body.code or "").strip() == ""
        assert empty.body.nets == []

    def test_language_is_detected_from_the_body_element(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        assert project.pous["Starter"].body.language == "ST"
        assert project.pous["Wiring"].body.language == "LD"
        # project.py fills the POU-level field from the same source.
        loaded = _load(plcopen_file)
        assert loaded.pous["Starter"].language == "ST"
        assert loaded.pous["Wiring"].language == "LD"


class TestGraphicalRendering:
    def test_ld_body_produces_a_net_with_block_and_pins(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        wiring = project.pous["Wiring"]
        assert len(wiring.body.nets) == 1
        block = wiring.body.nets[0].nodes[0]
        assert block.type_name == "MC_Power"
        assert block.instance == "MC_1"
        assert block.params["Axis"] == "TopCutter"
        assert block.params["Enable"] == "Enable_Cmd"

    def test_contact_and_coil_are_rendered(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        code = project.pous["Wiring"].body.code
        assert "[NO Enable_Cmd]" in code
        assert "-> Servo_On" in code

    def test_rendered_text_is_compact(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        code = project.pous["Wiring"].body.code
        assert "MC_Power(MC_1)" in code
        assert len(code.splitlines()) <= 4


class TestTypes:
    def test_struct_members(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        cam = project.types["CamStruct"]
        assert cam.kind == "struct"
        assert {m.name for m in cam.members} == {"PartLength", "KnifeDiameter"}

    def test_enum_values(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        mode = project.types["ModeEnum"]
        assert mode.kind == "enum"
        assert [v["name"] for v in mode.enum_values] == ["Off", "On"]

    def test_axis_ref_struct(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        axis = project.types["AXIS_REF"]
        assert [m.name for m in axis.members] == ["AxisNum"]


class TestTasksAndGlobals:
    def test_tasks_are_found_inside_resource(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        assert len(project.tasks) == 2

    def test_program_instances_are_attached_to_their_task(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        all_programs = {p for t in project.tasks.values() for p in t.programs}
        assert all_programs == {"Starter", "Empty"}

    def test_cxml_task_names_are_provisional_and_marked(self, plcopen_file: Path):
        """The exporter omits task names entirely, so ours must look synthetic."""

        project = _parse(plcopen_file)
        assert all("@" in name for name in project.tasks)

    def test_global_variables_with_address_and_comment(self, plcopen_file: Path):
        project = _parse(plcopen_file)
        assert len(project.global_vars) == 1
        var = project.global_vars[0]
        assert var.name == "PLCMODE_RUN"
        assert var.address == "%MX1.7.0"
        assert var.type_name == "BOOL"
        assert var.comment == "TRUE if running"
