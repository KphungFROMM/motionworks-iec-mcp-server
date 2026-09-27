"""Integration tests against real MotionWorks IEC exports.

These run only when ``MOTIONWORKS_SAMPLES`` points at a folder holding the
``PLCOpen XML Export``, ``Extended IEC 61131-2 Export`` and
``Motion Works IEC Program`` directories::

    MOTIONWORKS_SAMPLES="C:/Users/me/Desktop/MotionWorks IEC MCP" python -m pytest tests/ -v

They are the tests that actually protect the format decoders, because they run on
bytes MotionWorks wrote rather than on bytes we wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from motionworks_iec_mcp_server import project as pj
from motionworks_iec_mcp_server import server as S


def _xml(samples: Path) -> Path:
    return samples / "PLCOpen XML Export" / "TopCutter.xml"


def _ext(samples: Path, name: str = "TopCutter") -> Path:
    return samples / "Extended IEC 61131-2 Export" / name


def _native(samples: Path, name: str = "TopCutter") -> Path:
    return samples / "Motion Works IEC Program" / name


class TestXmlExport:
    def test_topcutter_pous_types_and_globals(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_xml(samples))
        assert "ServoHoming" in project.pous
        assert "ServoTaskSlow" in project.pous
        # The vendor data-type library is large and only the XML carries it.
        assert len(project.types) > 200
        assert "AXIS_REF" in project.types
        assert project.global_vars

    def test_global_variable_keeps_its_address_and_comment(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_xml(samples))
        run = next(v for v in project.global_vars if v.name == "PLCMODE_RUN")
        assert run.address == "%MX1.7.0"
        assert "running" in run.comment

    def test_ladder_body_is_split_into_many_networks(self, samples: Path | None):
        """A 23-block ladder must become 23 rungs, not one blob."""

        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_xml(samples))
        homing = project.pous["ServoHoming"]
        assert homing.language == "LD"
        assert len(homing.body.nets) >= 20

    def test_motion_block_parameters_are_resolved(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_xml(samples))
        net = project.pous["ServoTaskSlow"].body.nets[0]
        block = net.nodes[0]
        assert block.type_name == "MC_Power"
        assert block.params["Axis"] == "TopCutter"
        assert block.params["Enable"] == "EIP_FromCLX_Axis_SVON_Cmd"

    def test_tasks_are_present_but_unnamed(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_xml(samples))
        assert len(project.tasks) == 5
        assert all("@" in t.name for t in project.tasks.values())

    def test_rotaryknife_export_is_smaller_but_valid(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(samples / "PLCOpen XML Export" / "RotaryKnife_ASP_v350.xml")
        assert "RotaryKnifeMotion" in project.pous
        assert project.pous["RotaryKnifeDataPrep"].language == "ST"


class TestExtendedExport:
    def test_topcutter_pous_and_languages(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_ext(samples))
        assert "TopCutterCutControl" in project.pous
        assert project.pous["TopCutterCutControl"].language == "ST"
        assert project.processor_type == "MP2600iec"

    def test_task_names_and_programs(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_ext(samples))
        fast = project.tasks["FastTsk"]
        assert fast.interval == "T#4ms"
        assert fast.programs == ["TopCutterCutControl"]
        assert project.tasks["MedTsk"].programs == ["EIP_ToCLX", "ServoHoming"]

    def test_global_variables_with_addresses(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_ext(samples))
        assert len(project.global_vars) > 100
        by_name = {v.name: v for v in project.global_vars}
        assert by_name["PLCMODE_RUN"].address == "%MX1.7.0"
        assert by_name["PLC_SYS_TICK_CNT"].type_name == "DINT"

    def test_io_configuration(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_ext(samples))
        assert project.io_points
        groups = " ".join(p.group for p in project.io_points)
        assert "SGD7S" in groups

    def test_st_body_is_clean_source(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_ext(samples))
        code = project.pous["TopCutterCutControl"].body.code
        assert "PROGRAM" not in code
        assert "END_VAR" not in code
        # A ST POU has an ST body that actually does something.
        assert project.pous["TopCutterCutControl"].body.statements > 20
        assert "END_PROGRAM" not in code

    def test_ge_bodies_decode_to_blocks(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_ext(samples))
        slow = project.pous["ServoTaskSlow"]
        assert slow.language == "LD"
        names = {
            node.type_name
            for net in slow.body.nets
            for node in net.nodes
        }
        assert "MC_Power" in names
        assert "MC_ReadStatus" in names

    def test_no_unresolved_wire_pairs_silently_wrong(self, samples: Path | None):
        """The .GE decoder must never pair a pin it cannot justify.

        Its contract is precision over recall: whatever it reports must be
        correct. Here we assert the weaker, checkable property — that reported
        pin values are non-empty and are not the block's own instance name.
        """

        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_ext(samples))
        for pou in project.pous.values():
            for net in pou.body.nets:
                for node in net.nodes:
                    for pin, value in node.params.items():
                        assert value, f"{pou.name}:{node.type_name}.{pin} is empty"
                        assert value != node.instance


class TestNativeProject:
    def test_pou_and_task_map(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_native(samples))
        assert project.processor_type == "MP2600iec"
        assert project.tasks["FastTsk"].programs == ["TopCutterCutControl"]
        assert set(project.tasks["BG"].programs) == {
            "TopCutterCamSetup", "TopCutterFFCamSetup"
        }

    def test_no_code_is_claimed_from_a_native_project(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_native(samples))
        assert project.pous == {}

    def test_declared_pous_are_reported_as_unexported(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")
        project = pj.load(_native(samples))
        kinds = {d.kind for d in project.divergences}
        assert "pou_not_exported" in kinds


class TestCrossSourceOracle:
    """The PLCopen XML is the oracle for the ``.GE`` decoder.

    Both describe the same programs. Where the XML resolves a pin, the ``.GE``
    decoder must never disagree — it may only be silent.
    """

    PAIRS = [
        ("RK_DemoOnMP3300iec", "CamGen"),
        ("RK_DemoOnMP3300iec", "CamMotion"),
        ("TopCutter", "EIP_ToCLX"),
        ("TopCutter", "ServoTaskSlow"),
        ("RotaryKnife_ASP_v350", "RotaryKnifeCamCreation"),
        ("RotaryKnife_ASP_v350", "RotaryKnifeMotion"),
    ]

    def test_ge_never_contradicts_the_xml(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")

        checked = 0
        for project_name, pou_name in self.PAIRS:
            xml_path = samples / "PLCOpen XML Export" / f"{project_name}.xml"
            ge_path = _ext(samples, project_name) / f"{pou_name}.GE"
            if not xml_path.exists() or not ge_path.exists():
                continue

            xml_pins = _xml_pin_values(xml_path, pou_name)
            ge_pins = _ge_pin_values(ge_path)

            for instance, expected in xml_pins.items():
                actual = ge_pins.get(instance)
                if not actual:
                    continue
                for pin, value in expected.items():
                    if pin in actual:
                        assert actual[pin] == value, (
                            f"{project_name}/{pou_name}: {instance}.{pin} "
                            f"XML says {value!r}, .GE says {actual[pin]!r}"
                        )
                        checked += 1
        assert checked > 0, "the oracle comparison resolved no pins at all"


def _xml_pin_values(path: Path, pou_name: str) -> dict[str, dict[str, str]]:
    import xml.etree.ElementTree as ET

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    root = ET.parse(str(path)).getroot()
    for pou in root.iter():
        if local(pou.tag) != "pou" or pou.get("name") != pou_name:
            continue
        for body in pou:
            if local(body.tag) != "body":
                continue
            for lang in body:
                operands: dict[str, str] = {}
                for el in lang.iter():
                    if local(el.tag) in ("inVariable", "outVariable", "inOutVariable"):
                        var = next((c for c in el if local(c.tag) == "variable"), None)
                        operands[el.get("localId")] = (var.text or "").strip() if var is not None else ""
                out: dict[str, dict[str, str]] = {}
                for block in lang.iter():
                    if local(block.tag) != "block":
                        continue
                    instance = block.get("instanceName") or block.get("typeName")
                    pins: dict[str, str] = {}
                    for group in block:
                        if local(group.tag) not in ("inputVariables", "inOutVariables"):
                            continue
                        for var in group:
                            ref = None
                            for conn in var:
                                if local(conn.tag) == "connection":
                                    ref = conn.get("refLocalId")
                            value = operands.get(ref, "") if ref else ""
                            if value:
                                pins[var.get("formalParameter")] = value
                    out[instance] = pins
                return out
    return {}


def _ge_pin_values(path: Path) -> dict[str, dict[str, str]]:
    from motionworks_iec_mcp_server.parsers import graphical, st as stp
    from motionworks_iec_mcp_server.util import read_text

    _header, _decl, body = stp.split_pou_file(read_text(path))
    sheet = graphical.parse_ge(body)
    out: dict[str, dict[str, str]] = {}
    for net in graphical.render_sheet(sheet):
        for node in net.nodes:
            key = node.instance if node.instance and node.instance != "@" else node.type_name
            base, n = key, 1
            while key in out:
                n += 1
                key = f"{base}[{n}]"
            out[key] = dict(node.params)
    return out


class TestServerOnRealData:
    def test_end_to_end_flow(self, samples: Path | None):
        if samples is None:
            pytest.skip("MOTIONWORKS_SAMPLES not set")

        opened = json.loads(S.open_project(str(_xml(samples))))
        assert opened["pouCount"] >= 6

        tasks = json.loads(S.get_tasks(str(_native(samples))))
        fast = next(t for t in tasks["tasks"] if t["name"] == "FastTsk")
        assert fast["programs"] == ["TopCutterCutControl"]

        pou = json.loads(S.get_pou(str(_ext(samples)), "TopCutterCutControl"))
        assert pou["language"] == "ST"
        assert pou["task"] == "FastTsk"

        hits = json.loads(S.search_logic(str(_ext(samples)), "TopCutter_Homed"))
        assert hits["matchCount"] >= 1

        catalog = json.loads(S.list_fb_catalog(str(_xml(samples))))
        assert any(b["name"] == "MC_Power" for b in catalog["blocks"])

        validation = json.loads(S.validate_pou(
            str(_ext(samples)), "TopCutter_Homed := TRUE;"
        ))
        assert validation["ok"] is True

        bogus = json.loads(S.validate_pou(
            str(_ext(samples)), "TopCutter_Homedd := TRUE;"
        ))
        assert bogus["ok"] is False
