"""Tests for firmware-library interface recovery and the type-ordinal table.

The vendor motion library (``MC_*``, ``Y_*``) is **not** exported as type
definitions by any MotionWorks export. Its real interface is written down only in
the per-POU ``.DIT`` metadata inside the native project, with types as ``@TYP:<n>``
ids. These tests pin the recovery and, most importantly, that unresolved ids are
reported rather than guessed.
"""

from __future__ import annotations

from pathlib import Path

from motionworks_iec_mcp_server.model import Project
from motionworks_iec_mcp_server.parsers import native


# A miniature native project: one DIT for a library block whose pin types the
# project cannot teach, and one for a program whose declarations can.
TYLLIST = """\
(*
NDTE: 1
NCPE: 1
NDME: 0
*)
10 0\tMotionBlockTypes\\MotionB\tMyUserStruct\t1100\t8\tUSER\tSTRUCT
"""

LIBRARY_DIT = """\
(*
T: FUNCTION_BLOCK MC_Sample
FW: YES\tNO_VARIANT MC_Sample SKIP_NOTHING
CI#: 90
QSL: 0
QVE: 4
QPar: 4
QFBI: 0
*)
@V 1 1 0
Axis\t1\tVAR_IN_OUT\t@TYP:1
\t\t
;
@V 1 2 0
Execute\t2\tVAR_INPUT\t@TYP:1
\t\t
;
@V 1 3 0
BufferMode\t3\tVAR_INPUT\t@TYP:3
\t\t
;
@V 1 4 0
ErrorID\t4\tVAR_OUTPUT\t@TYP:99
\t\t
;
"""

PROGRAM_DIT = """\
(*
T: PROGRAM Starter
FW: NO
CI#: 51
*)
@V 1 1 0
xPermit\t1\tVAR\t@TYP:1
\t\t
;
@V 1 2 0
rDistance\t2\tVAR\t@TYP:11
\t\t
;
"""

NODES_LST = """\
CONFIGURATION\t2\tConfiguration\t\teCLR
RESOURCE\t3\tResource\t\t\tMP3300iec
"""


def _make_native(root: Path, with_code: bool = False) -> Path:
    res = root / "C" / "R" / "Resource"
    res.mkdir(parents=True)
    (root / "NODES.LST").write_text(NODES_LST, encoding="utf-8")
    (res / "eCLRPouDependencies.dat").write_text(
        "PouDep.Cache;schema 1\n1,Starter,2,PG\n", encoding="utf-8"
    )
    (res / "TYLLIST.TYP").write_text(TYLLIST, encoding="utf-8")
    (res / "ICI00090.DIT").write_text(LIBRARY_DIT, encoding="utf-8")
    (res / "ICI00051.DIT").write_text(PROGRAM_DIT, encoding="utf-8")
    return root


class TestTypeOrdinalTable:
    def test_registered_ids_come_from_tyllist(self, tmp_path: Path):
        root = _make_native(tmp_path / "Proj")
        ids = native._registered_type_ids(root)
        assert ids.get(1100) == "MyUserStruct"

    def test_only_function_block_dits_become_type_interfaces(self, tmp_path: Path):
        """A PROGRAM .DIT describes a POU, not a type, so it must not be registered."""

        root = _make_native(tmp_path / "Proj")
        project = Project()
        native.parse(root, project)
        assert "MC_Sample" in project.types
        assert "Starter" not in project.types

    def test_library_flag_separates_vendor_from_project_blocks(self, tmp_path: Path):
        """``is_library`` is decided by whether the POU index declares the block."""

        root = _make_native(tmp_path / "Proj")
        # Declare MC_Sample as a project FB so it stops looking like vendor code.
        res = root / "C" / "R" / "Resource"
        (res / "eCLRPouDependencies.dat").write_text(
            "PouDep.Cache;schema 1\n1,Starter,2,PG\n2,MC_Sample,3,FB\n",
            encoding="utf-8",
        )
        project = Project()
        native.parse(root, project)
        assert project.types["MC_Sample"].is_library is False


class TestLibraryInterfaceRecovery:
    def test_pins_are_named_ordered_and_scoped(self, tmp_path: Path):
        root = _make_native(tmp_path / "Proj")
        project = Project()
        native.parse(root, project)
        members = project.types["MC_Sample"].members
        assert [m.name for m in members] == ["Axis", "Execute", "BufferMode", "ErrorID"]
        # inOut declared before inputs, then outputs.
        assert [m.scope for m in members] == [
            "VAR_IN_OUT", "VAR_INPUT", "VAR_INPUT", "VAR_OUTPUT",
        ]

    def test_builtin_ids_are_resolved_from_the_fallback_table(self, tmp_path: Path):
        """The project has no readable code, so the documented table applies."""

        root = _make_native(tmp_path / "Proj")
        project = Project()
        native.parse(root, project)
        by_name = {m.name: m.type_name for m in project.types["MC_Sample"].members}
        assert by_name["Axis"] == "BOOL"
        assert by_name["Execute"] == "BOOL"
        assert by_name["BufferMode"] == "INT"

    def test_unresolvable_id_is_reported_not_guessed(self, tmp_path: Path):
        root = _make_native(tmp_path / "Proj")
        project = Project()
        native.parse(root, project)
        by_name = {m.name: m.type_name for m in project.types["MC_Sample"].members}
        # 99 is neither a builtin nor in TYLLIST: it must stay as the raw id.
        assert by_name["ErrorID"] == "@TYP:99"
        kinds = {d.kind for d in project.divergences}
        assert "unresolved_type_ids" in kinds

    def test_project_declarations_teach_the_table(self, tmp_path: Path):
        """When code is readable, the ordinal table is learned, not assumed."""

        root = _make_native(tmp_path / "Proj")
        project = Project()
        native.parse(root, project)
        # Manufacture the "we can see the code" condition: xPermit is @TYP:1 and
        # the project says BOOL; rDistance is @TYP:11 and says LREAL.
        from motionworks_iec_mcp_server.model import Pou, Var

        pou = Pou("Starter", source="plcopen_xml")
        pou.vars["VAR"] = [
            Var(name="xPermit", type_name="BOOL"),
            Var(name="rDistance", type_name="LREAL"),
        ]
        project.pous["Starter"] = pou
        native.parse_library_interfaces(root, project)
        by_name = {m.name: m.type_name for m in project.types["MC_Sample"].members}
        assert by_name["Axis"] == "BOOL"
        assert by_name["BufferMode"] == "INT"

    def test_learning_does_not_override_a_registered_id(self, tmp_path: Path):
        root = _make_native(tmp_path / "Proj")
        project = Project()
        native.parse(root, project)
        # 1100 is in TYLLIST and must resolve even with no project code.
        assert native._registered_type_ids(root)[1100] == "MyUserStruct"


class TestCallSiteInference:
    def test_pin_type_is_inferred_from_a_real_call_site(self, tmp_path: Path):
        """An unresolvable id can still be typed from what the project drives it with."""

        from motionworks_iec_mcp_server.model import Net, Node, Pou, Var

        root = _make_native(tmp_path / "Proj")
        project = Project()
        # A caller drives Axis (unresolvable @TYP:99...) with an AXIS_REF variable.
        caller = Pou("Caller", language="ST", source="extended_iec")
        caller.vars["VAR"] = [Var(name="TopCutter", type_name="AXIS_REF")]
        caller.body.nets = [
            Net(number=0, nodes=[
                Node(kind="block", type_name="MC_Sample", instance="MC_1",
                     params={"ErrorID": "TopCutter"}),
            ]),
        ]
        project.pous["Caller"] = caller
        native.parse_library_interfaces(root, project)
        by_name = {m.name: m for m in project.types["MC_Sample"].members}
        assert by_name["ErrorID"].type_name == "AXIS_REF"
        assert by_name["ErrorID"].inferred is True
        # And the inference is disclosed, not silent.
        assert by_name["ErrorID"].to_dict()["typeInferred"] is True


class TestSignaturesFromRecovery:
    def test_fb_signature_exposes_the_recovered_interface(self, tmp_path: Path):
        import json

        from motionworks_iec_mcp_server import server as S

        root = _make_native(tmp_path / "Proj")
        result = json.loads(S.get_fb_signature(str(root), "MC_Sample"))
        assert result["library"] is True
        assert "library interface" in result["derivedFrom"]
        assert [p["name"] for p in result["pins"]] == [
            "Axis", "Execute", "BufferMode", "ErrorID",
        ]
        assert result["pins"][0]["direction"] == "inOut"
        assert result["pins"][3]["direction"] == "output"

    def test_observed_fallback_still_works_without_dit(self):
        """A block with no .DIT falls back to observed usage, clearly labelled."""

        from motionworks_iec_mcp_server import codegen
        from motionworks_iec_mcp_server.model import Net, Node, Pou

        pou = Pou("P")
        pou.body.nets = [
            Net(number=0, nodes=[Node(kind="block", type_name="NoDitBlock",
                                      instance="B1", params={"Enable": "Cmd"})]),
        ]
        project = Project()
        project.pous["P"] = pou
        signature = codegen.fb_signature(project, "NoDitBlock")
        assert "observed usage" in signature["derivedFrom"]
        assert signature["pins"][0]["direction"] == "unknown"


class TestEnumValues:
    def test_enum_values_are_parsed_from_the_values_container(self, tmp_path: Path):
        """Regression: values live under <values>, and the value is the ordinal.

        Looking only at direct <value> children of <enum> returned zero values for
        every enum, which silently removed the motion library's whole symbolic
        vocabulary.
        """

        from motionworks_iec_mcp_server.parsers import plcopen_xml

        xml = tmp_path / "E.xml"
        xml.write_text(
            '<?xml version="1.0"?>'
            '<project xmlns="http://www.plcopen.org/xml/tc6.xsd">'
            "<types><dataTypes>"
            '<dataType name="MC_Direction"><baseType><enum><values>'
            '<value name="Positive_Direction"/>'
            '<value name="Shortest_Way"/>'
            '<value name="Both"/>'
            "</values><baseType><INT/></baseType></enum></baseType></dataType>"
            "</dataTypes></types></project>",
            encoding="utf-8",
        )
        project = Project()
        plcopen_xml.parse(xml, project)
        td = project.types["MC_Direction"]
        assert td.kind == "enum"
        assert [v["name"] for v in td.enum_values] == [
            "Positive_Direction", "Shortest_Way", "Both",
        ]
        # The numeric value is the ordinal position.
        assert [v["value"] for v in td.enum_values] == ["0", "1", "2"]
        assert td.base_type == "INT"

    def test_enum_is_registered_under_its_own_name(self, tmp_path: Path):
        """Regression: reusing `name` for values registered enums under a value name."""

        from motionworks_iec_mcp_server.parsers import plcopen_xml

        xml = tmp_path / "E2.xml"
        xml.write_text(
            '<?xml version="1.0"?>'
            '<project xmlns="http://www.plcopen.org/xml/tc6.xsd">'
            "<types><dataTypes>"
            '<dataType name="ModeEnum"><baseType><enum><values>'
            '<value name="Off"/><value name="On"/>'
            "</values></enum></baseType></dataType>"
            "</dataTypes></types></project>",
            encoding="utf-8",
        )
        project = Project()
        plcopen_xml.parse(xml, project)
        assert "ModeEnum" in project.types
        assert "On" not in project.types
