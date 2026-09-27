"""Tests for code validation and MotionWorks file emission."""

from __future__ import annotations

from pathlib import Path

from motionworks_iec_mcp_server import codegen
from motionworks_iec_mcp_server.model import Pou, Project, TypeDef, Var


def _project(**kwargs) -> Project:
    project = Project(name="Test")
    for name, value in kwargs.items():
        setattr(project, name, value)
    return project


class TestSymbolTable:
    def test_collects_globals_types_and_pous(self):
        project = _project(
            global_vars=[Var(name="GlobalFlag", type_name="BOOL")],
            types={"MyType": TypeDef(name="MyType")},
        )
        project.pous["MyProgram"] = Pou("MyProgram")
        symbols = codegen.project_symbols(project)
        assert {"GlobalFlag", "MyType", "MyProgram"} <= symbols

    def test_collects_pou_locals(self):
        pou = Pou("P")
        pou.vars["VAR"] = [Var(name="LocalFlag", type_name="BOOL")]
        project = _project(pous={"P": pou})
        assert "LocalFlag" in codegen.project_symbols(project)


class TestValidation:
    def test_valid_code_passes(self):
        project = _project(global_vars=[Var(name="Flag", type_name="BOOL")])
        result = codegen.validate(project, "Flag := TRUE;")
        assert result.ok

    def test_invented_tag_is_rejected(self):
        project = _project(global_vars=[Var(name="Flag", type_name="BOOL")])
        result = codegen.validate(project, "TypoFlag := TRUE;")
        assert not result.ok
        assert any(e["kind"] == "undeclared_tag" for e in result.errors)

    def test_locally_declared_symbols_are_accepted(self):
        project = _project()
        result = codegen.validate(
            project, "Counter := Counter + 1;",
            declared_vars="VAR\n  Counter : INT;\nEND_VAR",
        )
        assert result.ok

    def test_typed_literals_are_not_treated_as_symbols(self):
        project = _project(global_vars=[Var(name="Value", type_name="LREAL")])
        result = codegen.validate(project, "Value := LREAL#1.5;")
        assert result.ok

    def test_unknown_type_is_warned_about(self):
        project = _project()
        result = codegen.validate(
            project, "x := 1;", declared_vars="VAR\n  x : NoSuchType;\nEND_VAR"
        )
        assert any(w["kind"] == "unknown_type" for w in result.warnings)
        assert "NoSuchType" in result.unknown_types

    def test_duplicate_declaration_is_an_error(self):
        project = _project()
        result = codegen.validate(
            project, "x := 1;",
            declared_vars="VAR\n  x : BOOL;\n  x : BOOL;\nEND_VAR",
        )
        assert any(e["kind"] == "duplicate_declaration" for e in result.errors)

    def test_unknown_pin_on_a_declared_fb_is_caught(self):
        fb = TypeDef(
            name="MyBlock", kind="fb",
            members=[
                Var(name="Enable", type_name="BOOL", scope="VAR_INPUT"),
                Var(name="Done", type_name="BOOL", scope="VAR_OUTPUT"),
            ],
        )
        project = _project(types={"MyBlock": fb})
        result = codegen.validate(
            project,
            "MyInst.Enable := TRUE;\nMyInst.NotAPin := TRUE;",
            declared_vars="VAR\n  MyInst : MyBlock;\nEND_VAR",
        )
        assert any(e["kind"] == "unknown_pin" for e in result.errors)
        pin_error = next(e for e in result.errors if e["kind"] == "unknown_pin")
        assert pin_error["pin"] == "NotAPin"
        assert "Enable" in pin_error["detail"]

    def test_struct_member_access_is_not_flagged(self):
        struct = TypeDef(
            name="CamStruct", kind="struct",
            members=[Var(name="PartLength", type_name="LREAL")],
        )
        project = _project(
            types={"CamStruct": struct},
            global_vars=[Var(name="Cam", type_name="CamStruct")],
        )
        result = codegen.validate(project, "Cam.PartLength := LREAL#10.0;")
        assert result.ok, result.errors

    def test_comments_are_ignored(self):
        project = _project()
        result = codegen.validate(project, "(* BogusName := 1; *)")
        assert result.ok

    def test_empty_code_warns_rather_than_errors(self):
        project = _project()
        result = codegen.validate(project, "  ")
        assert result.ok
        assert any(w["kind"] == "empty_code" for w in result.warnings)

    def test_result_serialises(self):
        project = _project()
        payload = codegen.validate(project, "Nope := 1;").to_dict()
        assert payload["ok"] is False
        assert payload["errorCount"] >= 1
        assert payload["identifiersAreCaseInsensitive"] is True


class TestCaseInsensitivity:
    """IEC 61131-3 identifiers are not case sensitive.

    This is not theoretical: the RK_DemoOnMP3300iec sample project declares
    `Products` and compiles `products`. A case-sensitive validator therefore
    reports legal code as undeclared, which is the fastest way to make the tool
    worthless to an agent that is supposed to trust it.
    """

    def test_lowercase_reference_is_accepted(self):
        project = _project(global_vars=[Var(name="PLCMODE_RUN", type_name="BOOL")])
        result = codegen.validate(project, "plcmode_run := TRUE;")
        assert result.ok, result.errors

    def test_mixed_case_reference_is_accepted(self):
        project = _project(global_vars=[Var(name="PLCMODE_RUN", type_name="BOOL")])
        assert codegen.validate(project, "PlcMode_Run := TRUE;").ok

    def test_casing_difference_is_reported_as_a_style_warning(self):
        project = _project(global_vars=[Var(name="PLCMODE_RUN", type_name="BOOL")])
        result = codegen.validate(project, "plcmode_run := TRUE;")
        assert result.ok
        assert any(w["kind"] == "identifier_case" for w in result.warnings)
        assert "PLCMODE_RUN" in result.to_dict()["caseVariants"][0]

    def test_exact_casing_produces_no_warning(self):
        project = _project(global_vars=[Var(name="PLCMODE_RUN", type_name="BOOL")])
        result = codegen.validate(project, "PLCMODE_RUN := TRUE;")
        assert result.ok
        assert not result.case_variants

    def test_a_genuinely_unknown_symbol_is_still_an_error(self):
        project = _project(global_vars=[Var(name="PLCMODE_RUN", type_name="BOOL")])
        result = codegen.validate(project, "PLCMODE_RUNN := TRUE;")
        assert not result.ok
        assert any(e["kind"] == "undeclared_tag" for e in result.errors)

    def test_struct_member_access_is_case_insensitive(self):
        struct = TypeDef(name="S", kind="struct",
                         members=[Var(name="PartLength", type_name="LREAL")])
        project = _project(types={"S": struct},
                           global_vars=[Var(name="Cam", type_name="S")])
        assert codegen.validate(project, "cam.partlength := LREAL#1.0;").ok

    def test_qualified_name_case_variant_is_accepted(self):
        project = _project(global_vars=[Var(name="TopCutter_Homed", type_name="BOOL")])
        assert codegen.validate(project, "topcutter_homed := TRUE;").ok


class TestPinValidation:
    def test_pin_names_are_checked_case_insensitively(self):
        fb = TypeDef(name="Blk", kind="fb",
                     members=[Var(name="Enable", type_name="BOOL", scope="VAR_INPUT")])
        project = _project(types={"Blk": fb})
        result = codegen.validate(
            project, "b.enable := TRUE;",
            declared_vars="VAR\n  b : Blk;\nEND_VAR",
        )
        assert result.ok, result.errors
        # The casing difference is surfaced, and it is collected during the pin pass.
        assert any(w["kind"] == "identifier_case" for w in result.warnings)

    def test_an_invented_pin_is_an_error(self):
        fb = TypeDef(name="Blk", kind="fb",
                     members=[Var(name="Enable", type_name="BOOL", scope="VAR_INPUT")])
        project = _project(types={"Blk": fb})
        result = codegen.validate(
            project, "b.NotAPin := TRUE;",
            declared_vars="VAR\n  b : Blk;\nEND_VAR",
        )
        assert any(e["kind"] == "unknown_pin" for e in result.errors)

    def test_reference_pins_enable_checking_without_project_types(self):
        """An Extended-only project has no type definitions at all.

        Without the firmware reference the pin check silently did nothing, so a
        nonsense pin passed validation on exactly the projects that need it checked.
        """

        project = _project()      # no types, like an Extended-only export
        declared = "VAR\n  ax : MC_ReadActualPosition;\nEND_VAR"

        without = codegen.validate(project, "ax.NotAPin := TRUE;", declared_vars=declared)
        assert without.ok, "nothing can be checked without pin information"

        with_reference = codegen.validate(
            project, "ax.NotAPin := TRUE;", declared_vars=declared,
            reference_pins={"MC_ReadActualPosition": {"Axis", "Enable", "Valid"}},
        )
        assert not with_reference.ok
        assert any(e["kind"] == "unknown_pin" for e in with_reference.errors)

    def test_reference_pins_accept_a_real_pin(self):
        project = _project(global_vars=[Var(name="TopCutter", type_name="AXIS_REF")])
        result = codegen.validate(
            project, "ax.Axis := TopCutter;",
            declared_vars="VAR\n  ax : MC_ReadActualPosition;\nEND_VAR",
            reference_pins={"MC_ReadActualPosition": {"Axis", "Enable"}},
        )
        assert result.ok, result.errors


class TestFunctionBlockCalls:
    """`fbMove(Execute := xGo, Axis := TopCutter)` is the canonical call style.

    Regression: the formal parameter names were read as symbols and reported as
    undeclared, so ordinary MotionWorks code produced three bogus errors. Because the
    callee's interface is known, those names can be validated as pins instead.
    """

    def _project(self) -> Project:
        fb = TypeDef(name="MC_MoveRelative", kind="fb", members=[
            Var(name="Axis", type_name="AXIS_REF", scope="VAR_IN_OUT"),
            Var(name="Execute", type_name="BOOL", scope="VAR_INPUT"),
            Var(name="Distance", type_name="LREAL", scope="VAR_INPUT"),
            Var(name="Done", type_name="BOOL", scope="VAR_OUTPUT"),
        ])
        project = Project()
        project.types["MC_MoveRelative"] = fb
        project.global_vars.append(Var(name="TopCutter", type_name="AXIS_REF"))
        return project

    DECL = "VAR\n  fbMove : MC_MoveRelative;\n  xGo : BOOL;\n  rTarget : LREAL;\nEND_VAR"

    def test_named_parameters_are_not_undeclared_symbols(self):
        result = codegen.validate(
            self._project(),
            "fbMove(Execute := xGo, Distance := rTarget, Axis := TopCutter);",
            declared_vars=self.DECL,
        )
        assert result.ok, result.errors

    def test_a_wrong_pin_name_in_a_call_is_caught(self):
        """Previously impossible: the pin check only understood dotted assignment."""

        result = codegen.validate(
            self._project(),
            "fbMove(Execute := xGo, NotAPin := rTarget);",
            declared_vars=self.DECL,
        )
        assert not result.ok
        assert any(e["kind"] == "unknown_pin" for e in result.errors)
        pin_error = next(e for e in result.errors if e["kind"] == "unknown_pin")
        assert pin_error["pin"] == "NotAPin"

    def test_a_real_variable_after_the_assign_is_still_checked(self):
        result = codegen.validate(
            self._project(),
            "fbMove(Execute := xGo, Distance := NoSuchVar);",
            declared_vars=self.DECL,
        )
        assert any(e["kind"] == "undeclared_tag" for e in result.errors)

    def test_a_direct_block_type_call_is_recognised(self):
        result = codegen.validate(
            self._project(),
            "MC_MoveRelative(Execute := xGo, Axis := TopCutter);",
            declared_vars=self.DECL,
        )
        assert result.ok, result.errors

    def test_positional_arguments_are_still_checked_as_symbols(self):
        result = codegen.validate(
            self._project(), "fbMove(xGo, rTarget);", declared_vars=self.DECL
        )
        assert result.ok, result.errors

    def test_dotted_pin_assignment_still_works(self):
        project = self._project()
        assert codegen.validate(
            project, "fbMove.Execute := xGo;", declared_vars=self.DECL
        ).ok
        assert not codegen.validate(
            project, "fbMove.Nope := xGo;", declared_vars=self.DECL
        ).ok

    def test_plain_assignment_is_unaffected(self):
        result = codegen.validate(self._project(), "xGo := TRUE;", declared_vars=self.DECL)
        assert result.ok, result.errors


class TestRendering:
    def test_pou_source_shape(self):
        source = codegen.render_pou_source(
            name="Demo", pou_type="program", language="ST",
            declaration="VAR\n  x : BOOL;\nEND_VAR",
            code="x := TRUE;",
            description="Doc text.",
        )
        assert source.startswith("(*@PROPERTIES_EX@")
        assert "TYPE: POU" in source
        assert "IEC_LANGUAGE: ST" in source
        assert "PROGRAM Demo" in source
        assert "Doc text." in source
        assert "END_PROGRAM" in source

    def test_function_block_closing_keyword(self):
        source = codegen.render_pou_source(name="Fb", pou_type="function_block")
        assert "FUNCTION_BLOCK Fb" in source
        assert source.rstrip().endswith("END_FUNCTION_BLOCK")

    def test_bare_declaration_is_wrapped_in_var(self):
        source = codegen.render_pou_source(
            name="Demo", declaration="x : BOOL;"
        )
        assert "VAR\nx : BOOL;\nEND_VAR" in source

    def test_type_source_shape(self):
        source = codegen.render_type_source(
            name="CamStruct",
            members=[
                {"name": "PartLength", "type": "LREAL", "comment": "mm"},
                {"name": "Count", "type": "UDINT"},
            ],
        )
        assert "CamStruct : STRUCT" in source
        assert "PartLength" in source and "LREAL;" in source
        assert "(*  mm  *)" in source
        assert source.rstrip().endswith("END_TYPE")

    def test_empty_type_source_is_valid(self):
        source = codegen.render_type_source(name="Empty", members=[])
        assert "Empty : STRUCT" in source
        assert "END_STRUCT;" in source


class TestFbSignatures:
    def test_declared_type_is_used_when_available(self):
        fb = TypeDef(
            name="MyBlock", kind="fb", is_library=True,
            members=[Var(name="Enable", type_name="BOOL", scope="VAR_INPUT")],
        )
        project = _project(types={"MyBlock": fb})
        signature = codegen.fb_signature(project, "MyBlock")
        # A library interface recovered from .DIT metadata is reported as such, so
        # the caller knows it came from the project's firmware metadata rather than
        # from a project-authored declaration.
        assert "library interface" in signature["derivedFrom"]
        assert signature["library"] is True
        assert signature["pins"][0]["direction"] == "input"
        assert signature["pins"][0]["type"] == "BOOL"

    def test_project_authored_type_is_reported_as_declared(self):
        fb = TypeDef(
            name="MyBlock", kind="fb", is_library=False,
            members=[Var(name="Enable", type_name="BOOL", scope="VAR_INPUT")],
        )
        project = _project(types={"MyBlock": fb})
        signature = codegen.fb_signature(project, "MyBlock")
        assert signature["derivedFrom"] == "declared type"

    def test_observed_usage_is_used_for_library_blocks(self):
        from motionworks_iec_mcp_server.model import Net, Node

        pou = Pou("P")
        pou.body.nets = [
            Net(number=0, nodes=[Node(kind="block", type_name="MC_Power",
                                      instance="MC_1",
                                      params={"Enable": "Cmd", "Axis": "TopCutter"})]),
        ]
        project = _project(pous={"P": pou})
        signature = codegen.fb_signature(project, "MC_Power")
        assert "observed" in signature["derivedFrom"]
        assert {p["name"] for p in signature["pins"]} == {"Enable", "Axis"}
        assert signature["instances"] == ["MC_1"]

    def test_unknown_block_returns_none(self):
        assert codegen.fb_signature(_project(), "Nope") is None

    def test_catalog_counts_usage(self):
        from motionworks_iec_mcp_server.model import Net, Node

        pou = Pou("P")
        pou.body.nets = [
            Net(number=0, nodes=[Node(kind="block", type_name="MOVE", params={"IN1": "a"})]),
            Net(number=1, nodes=[Node(kind="block", type_name="MOVE", params={"IN1": "b"})]),
        ]
        project = _project(pous={"P": pou})
        catalog = codegen.fb_usage_catalog(project)
        assert catalog["MOVE"]["useCount"] == 2
        assert catalog["MOVE"]["pins"] == ["IN1"]
