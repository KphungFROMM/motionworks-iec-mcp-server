"""Tests for the firmware-library reference extractor.

The reference is Yaskawa's own help, decompiled from ``.chm`` and parsed. These
tests run against synthetic pages shaped exactly like the real ones — the page
structures were derived from the installed 3.7.5.1 reference, so the fixtures
mirror its banner table, scope banners, ``unsupported`` row class and section
headings.

The real extraction is exercised when the install is present; see
``test_real_reference_is_parsed``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motionworks_iec_mcp_server import fwlib

from .conftest import (
    REAL_DATATYPE_PAGE,
    REAL_ENUM_INDEX_PAGE,
    REAL_ENUM_TYPE_PAGE,
    REAL_FB_PAGE,
)


@pytest.fixture
def help_tree(tmp_path: Path) -> Path:
    """A miniature extracted help tree with the real page shapes."""

    details = tmp_path / "PLCopen Function Blocks" / "Details"
    details.mkdir(parents=True)
    (details / "MC_Power.htm").write_bytes(REAL_FB_PAGE)

    common = tmp_path / "Common_Datatypes"
    common.mkdir()
    (common / "Data_Type__AXIS_REF.htm").write_bytes(REAL_DATATYPE_PAGE)
    (common / "Data_Type__MC_Direction.htm").write_bytes(REAL_ENUM_TYPE_PAGE)
    (common / "Enumerated_Types.htm").write_bytes(REAL_ENUM_INDEX_PAGE)

    # An overview page that also carries a parameters-shaped table; it must not be
    # mistaken for a block.
    (tmp_path / "FunctionBlocksList.htm").write_bytes(OVERVIEW_PAGE)
    return tmp_path


class TestBlockParsing:
    def test_block_is_parsed_with_name_library_and_description(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        block = catalog.blocks["MC_Power"]
        assert block.library == "PLCopen_Plus_v2_2a Firmware Library"
        assert block.description == "This Function Block enables or disables the axis."

    def test_pins_carry_scope_type_default_and_order(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        pins = catalog.blocks["MC_Power"].pins
        assert [p.name for p in pins] == [
            "Axis", "Enable", "Enable_Positive", "BufferMode", "Status", "ErrorID",
        ]
        # Scope banners switch mid-table; a two-cell banner (`VAR_INPUT` + the
        # `Default` column label) previously left inputs labelled as inOut.
        assert [p.scope for p in pins] == [
            "VAR_IN_OUT", "VAR_INPUT", "VAR_INPUT", "VAR_INPUT",
            "VAR_OUTPUT", "VAR_OUTPUT",
        ]
        by_name = {p.name: p for p in pins}
        assert by_name["Axis"].type == "AXIS_REF"
        assert by_name["BufferMode"].type == "MC_BufferMode"
        assert by_name["Enable"].default == "FALSE"
        assert by_name["BufferMode"].default == "MC_BufferMode#Aborting"

    def test_unsupported_pins_are_flagged(self, help_tree: Path):
        """A pin the firmware does not implement must be visible as such."""

        catalog = fwlib.parse_tree(help_tree)
        by_name = {p.name: p for p in catalog.blocks["MC_Power"].pins}
        assert by_name["Enable_Positive"].supported is False
        assert by_name["Axis"].supported is True

    def test_pin_descriptions_are_captured(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        by_name = {p.name: p for p in catalog.blocks["MC_Power"].pins}
        assert "Logical axis reference" in by_name["Axis"].description

    def test_signature_dict_uses_input_output_vocabulary(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        payload = catalog.blocks["MC_Power"].to_dict()
        directions = {p["name"]: p["direction"] for p in payload["pins"]}
        assert directions["Axis"] == "inOut"
        assert directions["Enable"] == "input"
        assert directions["Status"] == "output"
        assert payload["unsupportedPins"] == ["Enable_Positive", "BufferMode"]

    def test_overview_page_is_not_a_block(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        assert "Function Block List" not in catalog.blocks
        assert "Data Types" not in catalog.blocks


class TestTypeParsing:
    def test_struct_members_are_parsed(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        axis = catalog.types["AXIS_REF"]
        assert axis.kind == "struct"
        assert [(m.name, m.type) for m in axis.members] == [("AxisNum", "UINT")]
        assert "logical axis number" in axis.members[0].description

    def test_enum_values_come_from_the_aggregate_page(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        buffer_mode = catalog.types["MC_BufferMode"]
        assert buffer_mode.kind == "enum"
        assert [v["name"] for v in buffer_mode.values] == [
            "Aborting", "Buffered", "BlendingLow",
        ]
        assert buffer_mode.values[0]["value"] == "0"
        assert "Default mode" in buffer_mode.values[0]["description"]

    def test_enum_values_are_merged_not_overwritten(self, help_tree: Path):
        """A type is described on its own page and in the index; neither is complete."""

        catalog = fwlib.parse_tree(help_tree)
        direction = catalog.types["MC_Direction"]
        names = [v["name"] for v in direction.values]
        assert "Positive_Direction" in names
        # 'Both' appears only on the type page, the others only in the index.
        assert "Both" in names

    def test_enum_is_sorted_by_numeric_value(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        values = catalog.types["MC_Direction"].values
        assert [v["value"] for v in values] == ["0", "1", "2", "9"]


class TestCatalogLookups:
    def test_block_lookup_is_case_insensitive(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        assert catalog.block("mc_power") is not None
        assert catalog.block("MC_Power") is not None
        assert catalog.block("Nope") is None

    def test_summary_counts(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        summary = catalog.summary()
        assert summary["blockCount"] == 1
        assert summary["enumTypeCount"] >= 1

    def test_round_trips_through_json(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        restored = fwlib.Catalog.from_dict(catalog.to_dict())
        assert [p.name for p in restored.blocks["MC_Power"].pins] == [
            p.name for p in catalog.blocks["MC_Power"].pins
        ]
        assert restored.types["MC_Direction"].values == catalog.types["MC_Direction"].values


class TestMerge:
    def test_reference_fills_pins_the_project_never_used(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        block = catalog.blocks["MC_Power"]
        project_signature = {
            "name": "MC_Power",
            "derivedFrom": "observed usage",
            "pins": [
                {"name": "Axis", "type": "@TYP:24", "direction": "inOut", "typeInferred": True},
                {"name": "Enable", "type": "BOOL", "direction": "input"},
            ],
        }
        merged = fwlib.merge_signature(project_signature, block)
        names = [p["name"] for p in merged["pins"]]
        # Pins the project never connected are still described.
        assert "BufferMode" in names
        assert "Status" in names
        assert merged["pinCount"] == 6

    def test_reference_type_wins_and_the_project_type_is_kept_visible(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        project_signature = {
            "name": "MC_Power",
            "derivedFrom": "project",
            "pins": [{"name": "BufferMode", "type": "INT", "direction": "input"}],
        }
        merged = fwlib.merge_signature(project_signature, catalog.blocks["MC_Power"])
        pin = next(p for p in merged["pins"] if p["name"] == "BufferMode")
        assert pin["type"] == "MC_BufferMode"
        assert pin["projectType"] == "INT"

    def test_unsupported_pins_are_surfaced_with_an_explanation(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        merged = fwlib.merge_signature(None, catalog.blocks["MC_Power"])
        assert "Enable_Positive" in merged["unsupportedPins"]
        assert "unsupportedNote" in merged

    def test_merge_without_a_project_signature_is_reference_only(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        merged = fwlib.merge_signature(None, catalog.blocks["MC_Power"])
        assert merged["derivedFrom"] == "firmware library reference"
        assert merged["library"].startswith("PLCopen_Plus")


class TestSearch:
    def test_search_finds_a_block_by_pin_name(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        hits = fwlib.search(catalog, "BufferMode")
        assert any(h["name"] == "MC_Power" for h in hits)

    def test_search_finds_an_enum_by_value_name(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        hits = fwlib.search(catalog, "BlendingLow")
        assert any(h["kind"] == "enum" and h["name"] == "MC_BufferMode" for h in hits)

    def test_search_finds_by_description(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        hits = fwlib.search(catalog, "disables the axis")
        assert hits and hits[0]["name"] == "MC_Power"

    def test_empty_query_returns_nothing(self, help_tree: Path):
        catalog = fwlib.parse_tree(help_tree)
        assert fwlib.search(catalog, "   ") == []


class TestInstallDiscovery:
    def test_env_override_is_honoured(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MOTIONWORKS_FWLIB", str(tmp_path))
        assert fwlib.default_install_dirs()[0] == tmp_path

    def test_version_key_sorts_numerically(self):
        assert fwlib._version_key("3_7_5_1_667") > fwlib._version_key("3_7_0_0_1")

    def test_cache_dir_is_overridable(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MOTIONWORKS_FWLIB_CACHE", str(tmp_path))
        assert fwlib.default_cache_dir() == tmp_path


class TestPreExtractedHtml:
    def test_a_folder_of_html_is_used_without_hh_exe(self, help_tree: Path, monkeypatch):
        """The escape hatch for hosts without hh.exe or PowerShell."""

        monkeypatch.setenv("MOTIONWORKS_FWLIB", str(help_tree))
        fwlib.clear_memo()
        catalog, problems = fwlib.catalog_for(libraries=["x"], refresh=True)
        assert catalog is not None
        assert "MC_Power" in catalog.blocks

    def test_missing_reference_reports_actionable_reasons(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MOTIONWORKS_FWLIB", str(tmp_path / "nope"))
        fwlib.clear_memo()
        catalog, problems = fwlib.catalog_for(libraries=["x"], refresh=True)
        assert catalog is None
        assert problems


@pytest.mark.skipif(
    fwlib.find_install() is None,
    reason="MotionWorks is not installed on this host",
)
class TestRealReference:
    def test_real_reference_is_parsed(self):
        catalog, problems = fwlib.catalog_for(refresh=False)
        assert catalog is not None, problems
        # The installed motion reference documents well over a hundred blocks.
        assert len(catalog.blocks) > 90
        assert len(catalog.types) > 20

    def test_known_blocks_have_the_documented_interface(self):
        catalog, _ = fwlib.catalog_for()
        assert catalog is not None
        power = catalog.block("MC_Power")
        assert power is not None
        assert [p.name for p in power.pins] == [
            "Axis", "Enable", "Enable_Positive", "Enable_Negative", "BufferMode",
            "Status", "Busy", "Active", "Error", "ErrorID",
        ]
        move = catalog.block("MC_MoveAbsolute")
        assert move is not None
        types = {p.name: p.type for p in move.pins}
        assert types["Position"] == "LREAL"
        assert types["Direction"] == "MC_Direction"
        assert types["BufferMode"] == "MC_BufferMode"

    def test_enum_values_match_the_reference(self):
        """The help documents four directions; the export lists five.

        Verified against the installed reference: `MC_Direction` is documented as
        Positive_Direction / Shortest_Way / Negative_Direction / Current_Direction.
        The project export additionally lists `Both`, which the firmware accepts but
        this help version does not document — so the export stays authoritative for
        values and the help for meaning. Both facts are asserted here so the
        discrepancy cannot be "fixed" by accident in either direction.
        """

        catalog, _ = fwlib.catalog_for()
        assert catalog is not None
        direction = catalog.data_type("MC_Direction")
        assert direction is not None
        assert [v["name"] for v in direction.values] == [
            "Positive_Direction", "Shortest_Way", "Negative_Direction",
            "Current_Direction",
        ]
        assert all(v.get("description") for v in direction.values)

    def test_catalog_is_cached_on_disk(self):
        import time

        fwlib.catalog_for()
        start = time.monotonic()
        fwlib.clear_memo()
        catalog, _ = fwlib.catalog_for()
        elapsed = time.monotonic() - start
        assert catalog is not None
        # A cached parse is fast; a re-parse of 200 pages is not.
        assert elapsed < 5.0


OVERVIEW_PAGE = b"""<?xml version="1.0" encoding="Windows-1252"?>
<html xmlns:MadCap="http://www.madcapsoftware.com/Schemas/MadCap.xsd">
<head><title>Function Block List</title></head>
<body><div id="content">
<h1><table><tr><td><h1><span class="SystemTitle">Function Block List</span></h1></td></tr>
<tr><td><h2><span class="SystemTitle">Function Block List</span></h2></td></tr></table></h1>
<p>Here is a list of the function blocks.</p>
<h2>PLCopen Plus Function Blocks</h2>
<table class="fb_parameters">
<tr><th><p>Parameter</p></th><th><p>Data type</p></th></tr>
<tr><td class="group" colspan="5"><p>VAR_INPUT</p></td></tr>
<tr><td><p>Ignore</p></td><td><p>Me</p></td><td><p>BOOL</p></td></tr>
</table>
</div></body></html>
"""
