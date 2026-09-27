"""Tests for the Structured Text and declaration parsers.

The declaration parser is where the subtlest bug in this codebase lived: the
statement is split on ``;`` before matching, so a pattern that *requires* the
trailing semicolon drops every declaration in the file and yields a silently
empty variable table. These tests pin that behaviour down.
"""

from __future__ import annotations

from motionworks_iec_mcp_server.parsers import st
from motionworks_iec_mcp_server.parsers.st import (
    count_statements,
    extract_identifiers,
    parse_declarations,
    parse_dit_declarations,
    parse_pou_header,
    split_pou_file,
)
from motionworks_iec_mcp_server.util import IEC_KEYWORDS


SIMPLE = """
VAR
    xPermit : BOOL := FALSE;
    iState : INT := 0;
END_VAR
"""

WITH_ADDRESS = """
VAR_GLOBAL
    PLC_SYS_TICK_CNT	AT %MD1.0 :	DINT;
    PLCMODE_RUN	AT %MX1.7.0 :	BOOL;(**)
    AXIS1_SI1_POT	AT %IX53376.0 :	BOOL;(*POT, default on pin #7*)
END_VAR
"""

MULTI_BLOCK = """
VAR_INPUT
    Execute : BOOL;
END_VAR
VAR_OUTPUT
    Done : BOOL;
    ErrorID : UINT;
END_VAR
VAR_EXTERNAL
    TopCutter : AXIS_REF;(*Servo axis*)
END_VAR
"""

ARRAYS = """
VAR
    Buffer : ARRAY [0..9] OF INT;
    Cam    : ARRAY[0..49] OF LREAL := 0.0;
END_VAR
"""


class TestDeclarations:
    def test_simple_declarations_are_parsed(self):
        found = parse_declarations(SIMPLE)
        names = [v.name for v in found["VAR"]]
        assert names == ["xPermit", "iState"]
        assert found["VAR"][0].type_name == "BOOL"
        assert found["VAR"][0].initial == "FALSE"
        assert found["VAR"][1].type_name == "INT"

    def test_trailing_semicolon_is_optional(self):
        """Regression: statements are split on ';', so it must not be required."""

        found = parse_declarations("VAR\n\tFlag : BOOL;\nEND_VAR")
        assert [v.name for v in found["VAR"]] == ["Flag"]
        assert found["VAR"][0].type_name == "BOOL"

    def test_address_is_separated_from_type(self):
        found = parse_declarations(WITH_ADDRESS)
        vars_ = {v.name: v for v in found["VAR_GLOBAL"]}
        assert vars_["PLC_SYS_TICK_CNT"].address == "%MD1.0"
        # The ':' that follows the address must not leak into the type name.
        assert vars_["PLC_SYS_TICK_CNT"].type_name == "DINT"
        assert vars_["PLCMODE_RUN"].address == "%MX1.7.0"
        assert vars_["PLCMODE_RUN"].type_name == "BOOL"
        assert vars_["AXIS1_SI1_POT"].address == "%IX53376.0"

    def test_inline_comments_are_attached(self):
        found = parse_declarations(WITH_ADDRESS)
        vars_ = {v.name: v for v in found["VAR_GLOBAL"]}
        assert "POT" in vars_["AXIS1_SI1_POT"].comment
        # A bare (**) carries no information and must not become a comment.
        assert vars_["PLCMODE_RUN"].comment == ""

    def test_multiple_scopes(self):
        found = parse_declarations(MULTI_BLOCK)
        assert sorted(found) == ["VAR_EXTERNAL", "VAR_INPUT", "VAR_OUTPUT"]
        assert found["VAR_OUTPUT"][1].name == "ErrorID"
        assert found["VAR_OUTPUT"][1].type_name == "UINT"

    def test_arrays_keep_base_type_and_dimensions(self):
        found = parse_declarations(ARRAYS)
        buf = found["VAR"][0]
        assert buf.type_name == "INT"
        assert buf.dimensions == "0..9"
        cam = found["VAR"][1]
        assert cam.type_name == "LREAL"
        assert cam.initial == "0.0"

    def test_unclosed_block_is_ignored(self):
        assert parse_declarations("VAR\n    x : BOOL;\n") == {}

    def test_empty_input(self):
        assert parse_declarations("") == {}


class TestDitDeclarations:
    def test_rows_are_parsed_with_slot_and_scope(self):
        text = (
            "(*\nT: FUNCTION_BLOCK CamGenerator\n*)\n"
            "@V 1 6 0\n"
            "CamData\t1\tVAR_IN_OUT\t@TYP:1306\n"
            "@V 1 12 0\n"
            "Execute\t3\tVAR_INPUT\t@TYP:1\n"
        )
        found = parse_dit_declarations(text)
        assert found["VAR_IN_OUT"][0].name == "CamData"
        assert found["VAR_IN_OUT"][0].type_name == "@TYP:1306"
        assert found["VAR_INPUT"][0].name == "Execute"

    def test_count_lines_are_not_treated_as_rows(self):
        text = "@V 1 6 0\nCamData\t1\tVAR_IN_OUT\t@TYP:1306\n"
        found = parse_dit_declarations(text)
        assert len(found["VAR_IN_OUT"]) == 1


class TestPouHeader:
    def test_program(self):
        assert parse_pou_header("PROGRAM Start\n") == ("program", "Start", "")

    def test_function_block(self):
        assert parse_pou_header("FUNCTION_BLOCK MyFb\n") == ("function_block", "MyFb", "")

    def test_function_with_return_type(self):
        assert parse_pou_header("FUNCTION Scale : LREAL\n") == ("function", "Scale", "LREAL")

    def test_missing_header(self):
        assert parse_pou_header("(* nothing here *)") == ("", "", "")


class TestSplitPouFile:
    def test_worksheet_region_is_separated(self):
        text = (
            "PROGRAM P\n"
            "VAR\n\tx : BOOL;\nEND_VAR\n"
            "(*@KEY@: WORKSHEET\nNAME: P\n*)\n"
            "x := TRUE;\n"
            "(*@KEY@: END_WORKSHEET *)\n"
            "END_PROGRAM\n"
        )
        header, decl, body = split_pou_file(text)
        assert "PROGRAM P" in header
        assert "x : BOOL;" in decl
        assert "x := TRUE;" in body
        assert "END_PROGRAM" not in body

    def test_body_without_worksheet_region(self):
        text = "PROGRAM P\nVAR\n\tx : BOOL;\nEND_VAR\nx := TRUE;\nEND_PROGRAM\n"
        _header, decl, body = split_pou_file(text)
        assert "x : BOOL;" in decl
        assert "x := TRUE;" in body.strip()


class TestStatementCounting:
    def test_counts_statements_not_comments(self):
        code = "(* a comment *)\nx := 1;\ny := 2;   (* trailing *)\n"
        assert count_statements(code) == 2

    def test_empty(self):
        assert count_statements("") == 0

    def test_comment_only(self):
        assert count_statements("(* nothing *)\n") == 0


class TestIdentifierExtraction:
    def test_keywords_are_excluded(self):
        code = "IF Flag THEN x := TRUE; END_IF;"
        found = extract_identifiers(code, IEC_KEYWORDS)
        assert "Flag" in found
        assert "x" in found
        assert "IF" not in found
        assert "TRUE" not in found

    def test_qualified_names_are_kept(self):
        found = extract_identifiers("Products.Sensor.Bit := Master.Position;", IEC_KEYWORDS)
        assert "Products.Sensor.Bit" in found
        assert "Master.Position" in found
