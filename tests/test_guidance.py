"""Tests for the "export this first" guidance.

A native MotionWorks project looks like the real thing and is the most natural path
to point at, but its code is a compound binary. An agent handed one has two bad
options: report "no source available", or invent code. These tests pin the third
option — the loader says what is missing, what that costs, how to produce it, and
where the export already is if it exists nearby.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motionworks_iec_mcp_server import guidance
from motionworks_iec_mcp_server.model import Project, SourceKind


class TestStateDetection:
    def test_native_only_is_blocking(self, native_project: Path):
        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        project.name = "SampleNative"

        payload = guidance.export_guidance(project, native_project)
        assert payload["issue"] == "no_export_found"
        assert payload["severity"] == "blocking"
        # It must be explicit that source code is simply not obtainable here.
        assert any("source code" in item for item in payload["whatYouCannotAnswer"])
        assert payload["whatIsStillAvailable"]
        assert payload["howToExport"]["plcopenXml"]
        assert payload["howToExport"]["extendedIec"]

    def test_extended_only_asks_for_the_xml_export(self, extended_export: Path):
        project = Project()
        project.sources.append(SourceKind.EXTENDED_IEC.value)
        payload = guidance.export_guidance(project, extended_export)
        assert payload["issue"] == "plcopen_xml_missing"
        assert payload["severity"] == "degraded"
        assert "plcopenXml" in payload["howToExport"]
        assert "extendedIec" not in payload["howToExport"]

    def test_xml_only_asks_for_the_extended_export(self, plcopen_file: Path):
        project = Project()
        project.sources.append(SourceKind.PLCOPEN_XML.value)
        payload = guidance.export_guidance(project, plcopen_file)
        assert payload["issue"] == "extended_iec_missing"
        assert "extendedIec" in payload["howToExport"]
        # The reason has to be given, or nobody follows the advice.
        assert "I/O configuration" in payload["why"]

    def test_both_sources_means_no_guidance(self, tmp_path: Path):
        project = Project()
        project.sources.extend([SourceKind.PLCOPEN_XML.value,
                                SourceKind.EXTENDED_IEC.value])
        assert guidance.export_guidance(project, tmp_path) is None

    def test_steps_are_the_verified_ones(self, native_project: Path):
        """Transcribed from the installed help, so a rewrite must be deliberate."""

        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        payload = guidance.export_guidance(project, native_project)
        steps = " ".join(payload["howToExport"]["plcopenXml"])
        assert "File" in steps and "Export" in steps
        assert "Export PLCopen xml file" in steps
        # Yaskawa's own warning about the schema version.
        assert "V1.01" in steps
        assert "V0.99 has never been released officially" in steps

    def test_documentation_pointer_is_included(self, native_project: Path):
        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        payload = guidance.export_guidance(project, native_project)
        assert "WORKFLOW.md" in payload["documentation"]


class TestNearbyDiscovery:
    """Exports are almost never far away, and finding them beats describing them."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """The real-world layout: sibling export folders beside the native one."""

        xml_dir = tmp_path / "PLCOpen XML Export"
        xml_dir.mkdir()
        (xml_dir / "Widget.xml").write_text("<project/>", encoding="utf-8")

        ext_dir = tmp_path / "Extended IEC 61131-2 Export" / "Widget"
        ext_dir.mkdir(parents=True)
        (ext_dir / "Main.ST").write_text(
            "(*@PROPERTIES_EX@\n\tNAME: 'Main'\n*)\nPROGRAM Main\nEND_PROGRAM\n",
            encoding="utf-8",
        )

        native_dir = tmp_path / "Motion Works IEC Program" / "Widget"
        native_dir.mkdir(parents=True)
        (native_dir / "NODES.LST").write_text("x", encoding="utf-8")
        return tmp_path

    def test_finds_the_matching_xml_export(self, workspace: Path):
        found = guidance.find_nearby_exports("Widget", workspace / "Motion Works IEC Program" / "Widget")
        paths = {Path(item["path"]).name for item in found}
        assert "Widget.xml" in paths

    def test_finds_the_nested_extended_export(self, workspace: Path):
        """`Extended IEC 61131-2 Export/<Project>/` is one level below the container."""

        found = guidance.find_nearby_exports("Widget", workspace / "Motion Works IEC Program" / "Widget")
        kinds = {item["kind"] for item in found}
        assert SourceKind.EXTENDED_IEC.value in kinds

    def test_name_matches_rank_first(self, workspace: Path):
        other = workspace / "PLCOpen XML Export" / "Gadget.xml"
        other.write_text("<project/>", encoding="utf-8")
        found = guidance.find_nearby_exports("Widget", workspace / "Motion Works IEC Program" / "Widget")
        assert Path(found[0]["path"]).name == "Widget.xml"
        assert found[0]["nameMatch"] is True

    def test_a_container_directory_is_not_reported_as_an_export(self, workspace: Path):
        """Regression: `is_extended_export` uses rglob and matched the workspace root.

        Suggesting the workspace root merges every project on disk, which is exactly
        the noise that makes a divergence list useless.
        """

        found = guidance.find_nearby_exports("Widget", workspace / "Motion Works IEC Program" / "Widget")
        reported = {str(Path(item["path"]).resolve()).lower() for item in found}
        assert str(workspace.resolve()).lower() not in reported
        assert str((workspace / "Extended IEC 61131-2 Export").resolve()).lower() not in reported

    def test_single_project_export_recognises_a_real_export(self, workspace: Path):
        assert guidance._is_single_project_export(
            workspace / "Extended IEC 61131-2 Export" / "Widget"
        )

    def test_single_project_export_rejects_a_container(self, workspace: Path):
        assert not guidance._is_single_project_export(workspace)
        assert not guidance._is_single_project_export(
            workspace / "Extended IEC 61131-2 Export"
        )

    def test_suggested_path_is_the_missing_kind(self, workspace: Path):
        """Regression: with the XML already loaded it suggested that same XML file."""

        project = Project()
        project.sources.append(SourceKind.PLCOPEN_XML.value)
        project.name = "Widget"
        project.source_paths = {
            SourceKind.PLCOPEN_XML.value: str(workspace / "PLCOpen XML Export" / "Widget.xml")
        }
        payload = guidance.export_guidance(
            project, workspace / "PLCOpen XML Export" / "Widget.xml"
        )
        assert payload["issue"] == "extended_iec_missing"
        assert Path(payload["suggestedPath"]).name == "Widget"
        assert payload["suggestedPath"].endswith("Widget")

    def test_already_loaded_artifacts_are_marked(self, workspace: Path):
        xml_path = workspace / "PLCOpen XML Export" / "Widget.xml"
        project = Project()
        project.sources.append(SourceKind.PLCOPEN_XML.value)
        project.name = "Widget"
        project.source_paths = {SourceKind.PLCOPEN_XML.value: str(xml_path)}

        payload = guidance.export_guidance(project, xml_path)
        loaded = [item for item in payload["foundNearby"] if item.get("alreadyLoaded")]
        assert loaded and Path(loaded[0]["path"]).name == "Widget.xml"

    def test_native_only_suggestion_is_a_usable_path(self, workspace: Path):
        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        project.name = "Widget"
        payload = guidance.export_guidance(
            project, workspace / "Motion Works IEC Program" / "Widget"
        )
        suggested = Path(payload["suggestedPath"])
        assert suggested.exists()
        # And pointing at it must actually load something.
        from motionworks_iec_mcp_server import project as pj

        loaded = pj.load(suggested, use_cache=False)
        assert loaded.sources

    def test_one_suggestion_per_missing_kind(self, workspace: Path):
        """Both exports exist nearby, so both are offered — one per kind, no repeats."""

        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        project.name = "Widget"
        payload = guidance.export_guidance(
            project, workspace / "Motion Works IEC Program" / "Widget"
        )
        paths = payload["suggestedPaths"]
        assert len(paths) == 2
        assert any(p.endswith("Widget.xml") for p in paths)
        assert any(Path(p).name == "Widget" for p in paths)
        # suggestedPath stays as the first, for callers that read only one.
        assert payload["suggestedPath"] == paths[0]

    def test_next_step_does_not_claim_two_calls_merge(self, workspace: Path):
        """Opening two paths separately gives two partial views, not a merged one.

        The advice has to say so, or a user follows it and wonders why the second
        call lost the first call's types.
        """

        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        project.name = "Widget"
        payload = guidance.export_guidance(
            project, workspace / "Motion Works IEC Program" / "Widget"
        )
        assert "one path" in payload["nextStep"].lower()
        assert "one folder" in payload["nextStep"].lower()

    def test_no_name_match_still_reports_what_is_nearby(self, workspace: Path):
        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        project.name = "SomethingElse"
        payload = guidance.export_guidance(
            project, workspace / "Motion Works IEC Program" / "Widget"
        )
        assert "suggestedPath" not in payload
        assert payload["foundNearby"]
        assert "no export matching" in payload["nextStep"].lower()

    def test_nothing_nearby_is_handled(self, tmp_path: Path):
        lonely = tmp_path / "Nowhere" / "Thing"
        lonely.mkdir(parents=True)
        project = Project()
        project.sources.append(SourceKind.NATIVE.value)
        project.name = "Thing"
        payload = guidance.export_guidance(project, lonely)
        assert payload["issue"] == "no_export_found"
        assert payload["nextStep"]
        assert "foundNearby" not in payload or payload["foundNearby"] == []


class TestToolWiring:
    def test_open_project_attaches_guidance(self, native_project: Path):
        from motionworks_iec_mcp_server import server as S

        payload = _payload(S.open_project(str(native_project)))
        assert payload["exportGuidance"]["issue"] == "no_export_found"

    def test_get_sources_attaches_guidance(self, native_project: Path):
        from motionworks_iec_mcp_server import server as S

        payload = _payload(S.get_sources(str(native_project)))
        assert payload["exportGuidance"]["issue"] == "no_export_found"

    def test_guidance_absent_when_nothing_is_missing(self, tmp_path: Path):
        """Both exports in one folder: no instruction needed, so none is given."""

        from motionworks_iec_mcp_server import server as S
        from tests.conftest import PLCOPEN_XML, STARTER_ST

        root = tmp_path / "Both"
        root.mkdir()
        (root / "Sample.xml").write_text(PLCOPEN_XML, encoding="utf-8")
        (root / "Main.ST").write_text(STARTER_ST, encoding="utf-8")

        payload = _payload(S.open_project(str(root)))
        assert "exportGuidance" not in payload

    def test_a_guidance_failure_does_not_break_the_tool(self, native_project: Path, monkeypatch):
        from motionworks_iec_mcp_server import server as S

        def boom(*_args, **_kwargs):
            raise RuntimeError("discovery exploded")

        monkeypatch.setattr(guidance, "export_guidance", boom)
        payload = _payload(S.open_project(str(native_project)))
        # The project still loads; only the advice degrades.
        assert payload["exportGuidance"]["issue"] == "guidance_unavailable"
        assert "discovery exploded" in payload["exportGuidance"]["note"]


def _payload(raw: str) -> dict:
    import json

    return json.loads(raw)
