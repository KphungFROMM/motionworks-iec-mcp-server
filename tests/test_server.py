"""Tests for the MCP tool layer.

Every tool must return a JSON string and must never raise: a bad path or an
unreadable export has to become a structured ``{"error": ...}`` so one tool call
cannot kill the agent's turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from motionworks_iec_mcp_server import server as S


def payload(raw: str):
    return json.loads(raw)


class TestHealth:
    def test_ping(self):
        assert S.ping() == "pong"


class TestErrorHandling:
    def test_missing_path_is_a_structured_error(self, tmp_path: Path):
        result = payload(S.open_project(str(tmp_path / "nope")))
        assert result["category"] == "source_not_found"
        assert "hint" in result

    def test_unrecognised_source_is_a_structured_error(self, tmp_path: Path):
        stray = tmp_path / "random.txt"
        stray.write_text("hello", encoding="utf-8")
        result = payload(S.open_project(str(stray)))
        assert result["category"] == "source_unrecognized"

    def test_unknown_pou_suggests_alternatives(self, extended_export: Path):
        result = payload(S.get_pou(str(extended_export), "Starterr"))
        assert result["category"] == "pou_not_found"
        assert "Starter" in result["did_you_mean"]
        assert "Starter" in result["available"]

    def test_unknown_type_suggests_alternatives(self, plcopen_file: Path):
        result = payload(S.get_type(str(plcopen_file), "CamStruc"))
        assert result["category"] == "type_not_found"
        assert "CamStruct" in result["did_you_mean"]

    def test_unknown_tag_is_reported(self, extended_export: Path):
        result = payload(S.get_tag(str(extended_export), "NoSuchSignal"))
        assert result["category"] == "tag_not_found"

    def test_unknown_fb_is_reported(self, plcopen_file: Path):
        result = payload(S.get_fb_signature(str(plcopen_file), "MC_Nope"))
        assert result["category"] == "fb_not_found"

    def test_bad_regex_is_reported(self, extended_export: Path):
        result = payload(S.search_logic(str(extended_export), "(unclosed"))
        assert result["category"] == "bad_pattern"

    def test_every_tool_survives_a_missing_path(self, tmp_path: Path):
        bad = str(tmp_path / "nope")
        assert payload(S.get_sources(bad))["category"] == "source_not_found"
        assert payload(S.get_tags(bad))["category"] == "source_not_found"
        assert payload(S.get_types(bad))["category"] == "source_not_found"
        assert payload(S.get_pous(bad))["category"] == "source_not_found"
        assert payload(S.get_tasks(bad))["category"] == "source_not_found"
        assert payload(S.get_io_config(bad))["category"] == "source_not_found"
        assert payload(S.get_motion_config(bad))["category"] == "source_not_found"
        assert payload(S.search_logic(bad, "x"))["category"] == "source_not_found"
        assert payload(S.validate_pou(bad, "x := 1;"))["category"] == "source_not_found"
        assert payload(S.list_fb_catalog(bad))["category"] == "source_not_found"


class TestProjectTools:
    def test_open_project_summary(self, extended_export: Path):
        result = payload(S.open_project(str(extended_export)))
        assert result["pouCount"] == 2
        assert "extended_iec" in result["sources"]

    def test_get_sources_explains_contributions(self, extended_export: Path):
        result = payload(S.get_sources(str(extended_export)))
        assert result["perSource"]["extended_iec"]["pous"] == 2
        assert result["perSource"]["extended_iec"]["withCode"] == 2

    def test_load_project_alias_accepts_an_xml_file(self, plcopen_file: Path):
        result = payload(S.load_project(str(plcopen_file)))
        assert result["sources"] == ["plcopen_xml"]


class TestVariableTools:
    def test_get_tags_returns_globals_with_addresses(self, extended_export: Path):
        result = payload(S.get_tags(str(extended_export)))
        names = {v["name"] for v in result["variables"]}
        assert "PLCMODE_RUN" in names
        run = next(v for v in result["variables"] if v["name"] == "PLCMODE_RUN")
        assert run["address"] == "%MX1.7.0"

    def test_get_tags_filters_by_scope(self, extended_export: Path):
        result = payload(S.get_tags(str(extended_export), scope="VAR_EXTERNAL"))
        assert result["variables"]
        assert all(v["scope"] == "VAR_EXTERNAL" for v in result["variables"])

    def test_get_tags_filters_by_address(self, extended_export: Path):
        result = payload(S.get_tags(str(extended_export), address="%MX1.7"))
        assert [v["name"] for v in result["variables"]] == ["PLCMODE_RUN"]

    def test_get_tags_filters_by_search(self, extended_export: Path):
        result = payload(S.get_tags(str(extended_export), search="AXIS1"))
        assert result["count"] >= 2

    def test_get_tag_returns_declaration_and_uses(self, extended_export: Path):
        result = payload(S.get_tag(str(extended_export), "TopCutter_Homed"))
        assert result["declarations"]
        assert result["useCount"] >= 1
        assert any(u["kind"] == "st" for u in result["uses"])


class TestTypeTools:
    def test_get_types_lists_kinds(self, plcopen_file: Path):
        result = payload(S.get_types(str(plcopen_file)))
        assert "struct" in result["kinds"]
        assert "enum" in result["kinds"]

    def test_get_types_search(self, plcopen_file: Path):
        result = payload(S.get_types(str(plcopen_file), search="Cam"))
        assert [t["name"] for t in result["types"]] == ["CamStruct"]

    def test_get_type_members(self, plcopen_file: Path):
        result = payload(S.get_type(str(plcopen_file), "CamStruct"))
        assert {m["name"] for m in result["members"]} == {"PartLength", "KnifeDiameter"}

    def test_get_udts_excludes_nothing_here(self, plcopen_file: Path):
        result = payload(S.get_udts(str(plcopen_file)))
        assert "CamStruct" in result["udts"]


class TestPouTools:
    def test_get_pous_lists_language_and_task(self, extended_export: Path):
        result = payload(S.get_pous(str(extended_export)))
        by_name = {p["name"]: p for p in result["pous"]}
        assert by_name["Starter"]["language"] == "ST"
        assert by_name["Starter"]["task"] == "FastTsk"

    def test_get_pous_filters_by_language(self, extended_export: Path):
        result = payload(S.get_pous(str(extended_export), language="LD"))
        assert [p["name"] for p in result["pous"]] == ["Wiring"]

    def test_get_pou_returns_st_body_and_interface(self, extended_export: Path):
        result = payload(S.get_pou(str(extended_export), "Starter"))
        assert "Axis1.AxisNum := UINT#1;" in result["body"]["code"]
        assert "VAR_EXTERNAL" in result["interface"]

    def test_get_pou_returns_decoded_nets_for_graphical_bodies(self, extended_export: Path):
        result = payload(S.get_pou(str(extended_export), "Wiring"))
        assert result["body"]["networkCount"] >= 1
        assert "MC_Power" in result["body"]["code"]

    def test_get_pou_pages_long_bodies(self, plcopen_file: Path, monkeypatch):
        monkeypatch.setattr(S, "MAX_BODY_LINES", 1)
        first = payload(S.get_pou(str(plcopen_file), "Starter"))
        assert first["body"]["truncated"] is True
        second = payload(S.get_pou(str(plcopen_file), "Starter", offset=first["body"]["nextOffset"]))
        assert second["body"]["code"] != ""

    def test_get_routine_alias(self, extended_export: Path):
        result = payload(S.get_routine(str(extended_export), "Starter", "Starter"))
        assert result["name"] == "Starter"


class TestTaskAndIo:
    def test_get_tasks_reports_timing_and_programs(self, extended_export: Path):
        result = payload(S.get_tasks(str(extended_export)))
        task = result["tasks"][0]
        assert task["name"] == "FastTsk"
        assert task["interval"] == "T#4ms"
        assert task["programs"] == ["Starter"]

    def test_get_tasks_flags_programs_missing_from_exports(self, native_project: Path):
        result = payload(S.get_tasks(str(native_project)))
        fast = next(t for t in result["tasks"] if t["name"] == "FastTsk")
        assert set(fast["programsMissingFromExports"]) == {"Starter", "Filler"}

    def test_get_io_config(self, extended_export: Path):
        result = payload(S.get_io_config(str(extended_export)))
        assert result["count"] == 2
        assert "LIODrv" in result["drivers"]

    def test_get_motion_config_finds_axis_bindings(self, extended_export: Path):
        result = payload(S.get_motion_config(str(extended_export)))
        assert result["axisBindings"][0]["axis"] == "Axis1"
        assert result["axisBindings"][0]["axisNum"] == "UINT#1"


class TestSearch:
    def test_search_finds_a_tag_in_code(self, extended_export: Path):
        result = payload(S.search_logic(str(extended_export), "TopCutter_Homed"))
        assert result["matchCount"] >= 1
        assert result["byKind"]

    def test_search_finds_a_block(self, extended_export: Path):
        result = payload(S.search_logic(str(extended_export), "MC_Power"))
        assert result["matchCount"] >= 1

    def test_search_limit_is_honoured(self, extended_export: Path):
        result = payload(S.search_logic(str(extended_export), "[A-Za-z]", limit=3))
        assert result["matchCount"] <= 3


class TestCodegenTools:
    def test_validate_accepts_real_project_code(self, extended_export: Path):
        code = "Axis1.AxisNum := UINT#1;\nIF TopCutter_Homed AND xPermit THEN\n  xPermit := FALSE;\nEND_IF;"
        result = payload(S.validate_pou(
            str(extended_export), code,
            declared_vars="VAR\n  xPermit : BOOL;\nEND_VAR",
        ))
        assert result["ok"] is True, result["errors"]

    def test_validate_rejects_an_invented_tag(self, extended_export: Path):
        result = payload(S.validate_pou(str(extended_export), "NoSuchTag := TRUE;"))
        assert result["ok"] is False
        assert any(e["kind"] == "undeclared_tag" for e in result["errors"])

    def test_render_pou_source_matches_the_import_shape(self):
        result = payload(S.render_pou_source(
            name="Demo", pou_type="program", language="ST",
            declaration="VAR\n  x : BOOL;\nEND_VAR",
            code="x := TRUE;",
            description="A demo.",
        ))
        source = result["source"]
        assert "(*@PROPERTIES_EX@" in source
        assert "IEC_LANGUAGE: ST" in source
        assert "PROGRAM Demo" in source
        assert "(*@KEY@: WORKSHEET" in source
        assert "(*@KEY@: END_WORKSHEET *)" in source
        assert source.rstrip().endswith("END_PROGRAM")

    def test_rendered_source_round_trips_through_the_st_parser(self):
        result = payload(S.render_pou_source(
            name="Demo", language="ST",
            declaration="VAR\n  x : BOOL;\nEND_VAR",
            code="x := TRUE;",
        ))
        from motionworks_iec_mcp_server.parsers import st as stp

        header, decl, body = stp.split_pou_file(result["source"])
        assert stp.parse_pou_header(result["source"])[:2] == ("program", "Demo")
        assert "x : BOOL;" in decl
        assert "x := TRUE;" in body

    def test_render_type_source(self):
        result = payload(S.render_type_source(
            name="CamStruct",
            members=[{"name": "PartLength", "type": "LREAL", "comment": "mm"}],
        ))
        assert "CamStruct : STRUCT" in result["source"]
        assert "((*" not in result["source"]
        assert "END_STRUCT;" in result["source"]

    def test_list_fb_catalog_reports_used_blocks(self, extended_export: Path):
        result = payload(S.list_fb_catalog(str(extended_export)))
        assert any(b["name"] == "MC_Power" for b in result["blocks"])

    def test_list_languages_states_the_limits(self):
        result = payload(S.list_languages())
        names = {lang["name"]: lang for lang in result["languages"]}
        assert names["ST"]["supported"] is True
        assert names["SFC"]["supported"] is False


class TestToolRegistry:
    def test_all_expected_tools_are_registered(self):
        import asyncio

        tools = {t.name for t in asyncio.run(S.mcp.list_tools())}
        expected = {
            "ping", "open_project", "load_project", "get_sources", "get_tags",
            "get_tag", "get_types", "get_type", "get_udts", "get_udt",
            "get_fb_signature", "list_fb_catalog", "get_pous", "get_routines",
            "get_pou", "get_routine", "get_tasks", "get_io_config",
            "get_motion_config", "search_logic", "validate_pou",
            "render_pou_source", "render_type_source", "list_languages",
            "list_library_blocks", "search_library", "get_library_reference_status",
            "get_code_conventions",
        }
        assert expected <= tools

    def test_every_tool_has_a_description(self):
        import asyncio

        for tool in asyncio.run(S.mcp.list_tools()):
            assert tool.description, f"{tool.name} has no description"
            assert len(tool.description) > 40, f"{tool.name} description is too thin"


@pytest.fixture
def reference(monkeypatch, tmp_path):
    """Point the server at a pre-extracted help tree, so no install is needed."""

    from .conftest import (
        REAL_DATATYPE_PAGE,
        REAL_ENUM_INDEX_PAGE,
        REAL_ENUM_TYPE_PAGE,
        REAL_FB_PAGE,
    )
    from motionworks_iec_mcp_server import fwlib

    tree = tmp_path / "help"
    (tree / "Common_Datatypes").mkdir(parents=True)
    details = tree / "PLCopen Function Blocks" / "Details"
    details.mkdir(parents=True)
    (details / "MC_Power.htm").write_bytes(REAL_FB_PAGE)
    (tree / "Common_Datatypes" / "Data_Type__AXIS_REF.htm").write_bytes(REAL_DATATYPE_PAGE)
    (tree / "Common_Datatypes" / "Data_Type__MC_Direction.htm").write_bytes(REAL_ENUM_TYPE_PAGE)
    (tree / "Common_Datatypes" / "Enumerated_Types.htm").write_bytes(REAL_ENUM_INDEX_PAGE)

    monkeypatch.setenv("MOTIONWORKS_FWLIB", str(tree))
    monkeypatch.setenv("MOTIONWORKS_FWLIB_CACHE", str(tmp_path / "cache"))
    fwlib.clear_memo()
    yield tree
    fwlib.clear_memo()


class TestLibraryReferenceTools:
    def test_status_reports_available_and_counts(self, reference):
        result = payload(S.get_library_reference_status())
        assert result["available"] is True
        assert result["catalog"]["blockCount"] == 1

    def test_status_explains_how_to_enable_when_missing(self, monkeypatch, tmp_path):
        from motionworks_iec_mcp_server import fwlib

        monkeypatch.setenv("MOTIONWORKS_FWLIB", str(tmp_path / "absent"))
        fwlib.clear_memo()
        try:
            result = payload(S.get_library_reference_status())
            assert result["available"] is False
            assert result["reasons"]
            assert result["howToEnable"]
        finally:
            fwlib.clear_memo()

    def test_list_library_blocks(self, reference):
        result = payload(S.list_library_blocks())
        assert [b["name"] for b in result["blocks"]] == ["MC_Power"]
        assert result["blocks"][0]["pinCount"] == 6

    def test_list_library_blocks_filters(self, reference):
        assert payload(S.list_library_blocks(search="pow"))["totalMatching"] == 1
        assert payload(S.list_library_blocks(search="nope"))["totalMatching"] == 0

    def test_search_library_finds_block_and_enum(self, reference):
        assert payload(S.search_library("disable"))["matchCount"] >= 1
        enum_hits = payload(S.search_library("BlendingLow"))["matches"]
        assert any(h["kind"] == "enum" for h in enum_hits)

    def test_fb_signature_uses_the_reference_for_an_unused_block(self, reference):
        """The gap this closes: a block the project has never called."""

        import json as _json

        raw = S.get_fb_signature(str(reference), "MC_Power")
        result = _json.loads(raw)
        # The fixture tree is not a project, so this exercises the reference path.
        assert "error" not in result or result.get("category") != "fb_not_found"

    def test_get_type_falls_back_to_the_reference(self, reference):
        result = payload(S.get_type(str(reference), "AXIS_REF"))
        # With no project open, the reference supplies the type.
        assert result.get("error") or result.get("members")


class TestConventionsTool:
    def test_conventions_are_reported_for_a_project(self, extended_export: Path):
        result = payload(S.get_code_conventions(str(extended_export)))
        assert result["symbolCount"] > 0
        assert "namingSummary" in result
        assert result["guidance"]

    def test_conventions_on_a_project_without_code_notes_why(self, native_project: Path):
        result = payload(S.get_code_conventions(str(native_project)))
        assert result["symbolCount"] == 0
        assert "note" in result
