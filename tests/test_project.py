"""Tests for source detection, merging and divergence reporting.

The merge step is the core value of the server: no single export is complete, so
the loader has to combine them and be honest about what it could not reconcile.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from motionworks_iec_mcp_server import project as pj
from motionworks_iec_mcp_server.model import Pou, Project, Task
from motionworks_iec_mcp_server.project import _assign_tasks, _normalise_interval


class TestDetection:
    def test_detects_extended_export(self, extended_export: Path):
        assert pj.detect(extended_export) == ["extended_iec"]

    def test_detects_native_project(self, native_project: Path):
        assert pj.detect(native_project) == ["native"]

    def test_detects_plcopen_file(self, plcopen_file: Path):
        assert pj.detect(plcopen_file) == ["plcopen_xml"]

    def test_unknown_path_yields_nothing(self, tmp_path: Path):
        stray = tmp_path / "random.txt"
        stray.write_text("hello", encoding="utf-8")
        assert pj.detect(stray) == []


class TestLoading:
    def test_loads_extended_export(self, extended_export: Path):
        project = pj.load(extended_export)
        assert project.sources == ["extended_iec"]
        assert "Starter" in project.pous
        assert project.global_vars

    def test_loads_native_project(self, native_project: Path):
        project = pj.load(native_project)
        assert project.sources == ["native"]
        assert project.tasks["FastTsk"].programs == ["Starter", "Filler"]

    def test_loads_plcopen_file(self, plcopen_file: Path):
        project = pj.load(plcopen_file)
        assert project.sources == ["plcopen_xml"]
        assert "Wiring" in project.pous

    def test_missing_path_raises(self, tmp_path: Path):
        with pytest.raises(pj.SourceNotFoundError):
            pj.load(tmp_path / "nope")

    def test_unrecognised_path_raises(self, tmp_path: Path):
        stray = tmp_path / "random.txt"
        stray.write_text("hello", encoding="utf-8")
        with pytest.raises(pj.SourceUnrecognizedError):
            pj.load(stray)


class TestGraphicalDecoding:
    def test_ge_bodies_are_decoded_during_load(self, extended_export: Path):
        project = pj.load(extended_export)
        wiring = project.pous["Wiring"]
        assert wiring.language == "LD"
        assert wiring.body.nets, "the .GE body should have been decoded into nets"
        assert "MC_Power" in wiring.body.code

    def test_st_bodies_are_left_alone(self, extended_export: Path):
        project = pj.load(extended_export)
        assert project.pous["Starter"].body.nets == []


class TestTaskAssignment:
    def test_program_gets_its_task(self, extended_export: Path):
        project = pj.load(extended_export)
        assert project.pous["Starter"].task == "FastTsk"

    def test_xml_only_project_has_no_task_names(self, plcopen_file: Path):
        project = pj.load(plcopen_file)
        assert all("@" in t.name for t in project.tasks.values())


class TestMerge:
    def test_both_sources_are_merged(self, tmp_path: Path, extended_export: Path, plcopen_file: Path):
        """A folder holding both export forms: both must contribute."""

        combined = tmp_path / "Combined"
        combined.mkdir()
        shutil.copy(plcopen_file, combined / "SampleProject.xml")
        shutil.copytree(extended_export, combined / "SampleExtended")

        project = pj.load(combined)
        assert set(project.sources) == {"plcopen_xml", "extended_iec"}
        # The XML has Empty, which the Extended export does not.
        assert "Empty" in project.pous
        assert "Starter" in project.pous

    def test_empty_body_is_filled_from_the_other_source(
        self, tmp_path: Path, extended_export: Path, plcopen_file: Path
    ):
        """The XML ships an empty <ST> body for its own Empty POU; Starter is fine.

        The observable contract: a POU present in both exports ends up with a real
        body, and the ST body is taken from the plain-text Extended source because
        the XML wraps ST in XHTML and normalises its whitespace.
        """

        combined = tmp_path / "Combined"
        combined.mkdir()
        shutil.copy(plcopen_file, combined / "SampleProject.xml")
        shutil.copytree(extended_export, combined / "SampleExtended")

        project = pj.load(combined)
        starter = project.pous["Starter"]
        assert "Axis1.AxisNum := UINT#1;" in starter.body.code
        # The XML-only POU survives, with its genuinely empty body.
        assert (project.pous["Empty"].body.code or "").strip() == ""
        kinds = {d.kind for d in project.divergences}
        assert "pou_xml_only" in kinds

    def test_st_body_prefers_the_plain_text_source(
        self, tmp_path: Path, extended_export: Path, plcopen_file: Path
    ):
        """For Structured Text, the plain-text export beats the HTML-wrapped one."""

        combined = tmp_path / "Combined"
        combined.mkdir()
        shutil.copy(plcopen_file, combined / "SampleProject.xml")
        shutil.copytree(extended_export, combined / "SampleExtended")

        project = pj.load(combined)
        starter = project.pous["Starter"]
        # The Extended source declares this statement; the XML fixture does not.
        assert "Axis1.AxisNum := UINT#1;" in starter.body.code
        # Body provenance is reported separately from the record's own source.
        assert starter.body.source == "extended_iec"

    def test_graphical_body_prefers_the_plcopen_export(self, tmp_path: Path):
        """For LD/FBD the XML's real rungs must win over the partial .GE decoding.

        Regression: the ``.GE`` decoder emits one net per block and per coil, so it
        reports *more* nets than the XML while resolving far fewer pin values.
        Selecting a body by net count therefore swapped in the less accurate one.
        """

        from .conftest import GE_TWO_BLOCKS, PLCOPEN_XML

        combined = tmp_path / "Graphical"
        combined.mkdir()
        (combined / "SampleProject.xml").write_text(PLCOPEN_XML, encoding="utf-8")
        ext = combined / "SampleExtended"
        ext.mkdir()
        (ext / "Wiring.GE").write_text(GE_TWO_BLOCKS, encoding="utf-8")
        (ext / "PHYSHARDWARE.EXP").write_text("x", encoding="utf-8")

        project = pj.load(combined)
        wiring = project.pous["Wiring"]
        assert wiring.body.nets, "expected nets in the merged graphical body"
        assert wiring.body.source == "plcopen_xml"
        # The XML path resolves the real formal parameters; the .GE fixture does not
        # connect Enable, so an Enable pin proves the XML body was used.
        code = wiring.body.code
        assert "Enable=Enable_Cmd" in code

    def test_body_provenance_is_reported_separately_from_the_record_source(
        self, tmp_path: Path, extended_export: Path, plcopen_file: Path
    ):
        """A POU's metadata and its code can legitimately come from different files."""

        combined = tmp_path / "Prov"
        combined.mkdir()
        shutil.copy(plcopen_file, combined / "SampleProject.xml")
        shutil.copytree(extended_export, combined / "SampleExtended")

        project = pj.load(combined)
        starter = project.pous["Starter"]
        # ST prefers the plain-text Extended source...
        assert starter.body.source == "extended_iec"
        # ...even though the XML record supplied the richer metadata.
        assert "Axis1.AxisNum := UINT#1;" in starter.body.code

    def test_divergence_reports_pous_present_in_only_one_source(
        self, tmp_path: Path, extended_export: Path, plcopen_file: Path
    ):
        combined = tmp_path / "Combined"
        combined.mkdir()
        shutil.copy(plcopen_file, combined / "SampleProject.xml")
        shutil.copytree(extended_export, combined / "SampleExtended")

        project = pj.load(combined)
        details = " ".join(d.detail for d in project.divergences)
        assert "Empty" in details or "Wiring" in details

    def test_native_declares_pous_the_exports_lack(
        self, tmp_path: Path, native_project: Path
    ):
        """The native project lists POUs the export does not carry — must be reported."""

        combined = tmp_path / "Combined"
        combined.mkdir()
        shutil.copytree(native_project, combined / "SampleNative")

        project = pj.load(combined / "SampleNative")
        kinds = {d.kind for d in project.divergences}
        assert "pou_not_exported" in kinds
        detail = next(d.detail for d in project.divergences if d.kind == "pou_not_exported")
        assert "Starter" in detail


class TestCache:
    def test_repeated_load_returns_equal_result(self, extended_export: Path):
        first = pj.load(extended_export)
        second = pj.load(extended_export)
        assert first is second, "the parse cache should serve the second call"

    def test_cache_is_invalidated_when_a_file_changes(self, extended_export: Path):
        first = pj.load(extended_export)
        target = extended_export / "Starter.ST"
        text = target.read_text(encoding="utf-8")
        target.write_text(text + "\n(* touched *)\n", encoding="utf-8")
        second = pj.load(extended_export)
        assert second is not first

    def test_clear_cache_forces_a_reparse(self, extended_export: Path):
        first = pj.load(extended_export)
        pj.clear_cache()
        assert pj.load(extended_export) is not first


class TestSummary:
    def test_summary_is_json_serialisable(self, extended_export: Path):
        import json

        project = pj.load(extended_export)
        json.dumps(project.summary())      # must not raise

    def test_summary_reports_counts_and_languages(self, extended_export: Path):
        project = pj.load(extended_export)
        summary = project.summary()
        assert summary["pouCount"] == 2
        assert summary["languages"]["ST"] == 1
        assert summary["languages"]["LD"] == 1
        assert summary["taskCount"] == 1
        assert summary["ioCount"] == 2


class TestEmptyProject:
    def test_project_with_no_pous_is_still_valid(self, tmp_path: Path):
        root = tmp_path / "Bare"
        root.mkdir()
        (root / "PHYSHARDWARE.EXP").write_text("x", encoding="utf-8")
        project = pj.load(root)
        assert project.pous == {}
        assert project.summary()["pouCount"] == 0


class TestTaskIntervalNormalisation:
    """The two exports write intervals in incompatible notations.

    The PLCopen XML export writes `00:00:00.20` for 20 ms — the fractional digits
    *are* the millisecond count, not a decimal fraction of a second. The Extended
    export writes IEC literals (`T#20ms`). Recognising a task across the two requires
    normalising both, and reading the first as a decimal fraction silently turns
    20 ms into 200 ms, so no task would ever match.
    """

    @pytest.mark.parametrize("text,expected", [
        ("00:00:00.4", 4),
        ("00:00:00.20", 20),
        ("00:00:00.100", 100),
        ("00:00:01.0", 1000),
        ("00:00:02.0", 2000),
        ("T#4ms", 4),
        ("T#20ms", 20),
        ("T#100ms", 100),
        ("T#1000ms", 1000),
        ("T#1s", 1000),
        ("T#2.5s", 2500),
    ])
    def test_recognised_forms(self, text, expected):
        assert _normalise_interval(text) == expected

    @pytest.mark.parametrize("text", ["", "WarmStart", "garbage"])
    def test_unrecognised_forms_return_none(self, text):
        assert _normalise_interval(text) is None

    def test_equivalent_intervals_agree_across_notations(self):
        assert _normalise_interval("00:00:00.20") == _normalise_interval("T#20ms")
        assert _normalise_interval("00:00:00.100") == _normalise_interval("T#100ms")


class TestTaskAssignment:
    """Regression tests for task merging across the two exports.

    The bug these pin: provisional XML tasks were matched to named tasks by *any*
    overlap of their program lists. When two exports describe different generations
    of a project — which the samples really do, one naming `StraightCut`/`CamGen`
    where the other names `TopCutterCutControl` — that silently welded programs onto
    the wrong task, attaching `ServoTaskSlow` to `MedTsk`. Matching is now by
    priority **and normalised interval**, and a program-list disagreement is reported
    rather than merged.

    Fixtures build the *unassigned* project and each test decides when to assign:
    assignment is a one-shot step in `load()`, and running it twice is destructive by
    nature, because the provisional task has already been renamed and dropped and its
    program list is no longer there to attribute from.
    """

    def _project(self) -> Project:
        project = Project()
        project.sources.extend(["plcopen_xml", "extended_iec"])
        # From the Extended export: real names, IEC intervals.
        project.tasks["FastTsk"] = Task(name="FastTsk", interval="T#4ms", priority=0,
                                        programs=["TopCutterCutControl"])
        project.tasks["MedTsk"] = Task(name="MedTsk", interval="T#20ms", priority=3,
                                       programs=["EIP_ToCLX", "ServoHoming"])
        project.tasks["SlowTsk"] = Task(name="SlowTsk", interval="T#100ms", priority=7,
                                        programs=["ServoTaskSlow"])
        # From the PLCopen XML export: provisional names, its own interval notation.
        project.tasks["task@0@00:00:00.4"] = Task(
            name="task@0@00:00:00.4", interval="00:00:00.4", priority=0,
            programs=["StraightCut"])
        project.tasks["task@7@00:00:00.100"] = Task(
            name="task@7@00:00:00.100", interval="00:00:00.100", priority=7,
            programs=["EIP_ToCLX", "ServoTaskSlow"])
        project.tasks["task@10@00:00:01.0"] = Task(
            name="task@10@00:00:01.0", interval="00:00:01.0", priority=10,
            programs=["CamGen"])
        return project

    def _assigned(self) -> Project:
        project = self._project()
        _assign_tasks(project)
        return project

    def test_matching_task_is_renamed_and_the_duplicate_dropped(self):
        project = self._assigned()
        assert "task@0@00:00:00.4" not in project.tasks
        assert "task@7@00:00:00.100" not in project.tasks
        assert "FastTsk" in project.tasks and "SlowTsk" in project.tasks

    def test_unmatched_task_keeps_its_provisional_name(self):
        project = self._assigned()
        # Priority 10 / 1 s has no named counterpart, so it stays provisional rather
        # than being guessed onto some other task.
        assert "task@10@00:00:01.0" in project.tasks

    def test_programs_are_not_welded_onto_a_matched_task(self):
        """The core bug: `EIP_ToCLX` must not end up in `SlowTsk`."""

        project = self._assigned()
        assert project.tasks["SlowTsk"].programs == ["ServoTaskSlow"]
        assert project.tasks["MedTsk"].programs == ["EIP_ToCLX", "ServoHoming"]
        assert project.tasks["FastTsk"].programs == ["TopCutterCutControl"]

    def test_a_program_list_disagreement_is_reported(self):
        project = self._assigned()
        conflicts = [d for d in project.divergences if d.kind == "task_program_conflict"]
        assert conflicts
        assert any("SlowTsk" in d.detail for d in conflicts)

    def test_no_pou_points_at_a_task_that_does_not_exist(self):
        """A program must never be attributed to a provisional name that was renamed.

        Regression: attributions were built before renaming, so `StraightCut` pointed
        at `task@0@00:00:00.4`, a task that no longer existed by the time the answer
        was returned.
        """

        project = self._project()
        for name in ("StraightCut", "CamGen", "TopCutterCutControl", "ServoTaskSlow"):
            project.pous[name] = Pou(name)
        _assign_tasks(project)
        for pou in project.pous.values():
            if pou.task:
                assert pou.task in project.tasks, (
                    f"{pou.name} points at missing task {pou.task!r}"
                )

    def test_a_program_only_the_provisional_task_schedules_is_still_attributed(self):
        project = self._project()
        project.pous["CamGen"] = Pou("CamGen")
        project.pous["StraightCut"] = Pou("StraightCut")
        project.pous["EIP_ToCLX"] = Pou("EIP_ToCLX")
        _assign_tasks(project)
        # CamGen's task has no real name, so the provisional name is used — its
        # schedule is known even though its name is not.
        assert project.pous["CamGen"].task == "task@10@00:00:01.0"
        # StraightCut matched FastTsk by priority + interval.
        assert project.pous["StraightCut"].task == "FastTsk"
        # A real name always beats a provisional one.
        assert project.pous["EIP_ToCLX"].task == "MedTsk"

    def test_same_priority_but_different_interval_does_not_match(self):
        project = Project()
        project.tasks["Slow"] = Task(name="Slow", interval="T#100ms", priority=7,
                                     programs=["A"])
        project.tasks["task@7@00:00:00.20"] = Task(
            name="task@7@00:00:00.20", interval="00:00:00.20", priority=7,
            programs=["B"])
        _assign_tasks(project)
        assert "task@7@00:00:00.20" in project.tasks
        assert project.tasks["Slow"].programs == ["A"]

    def test_scheduling_survives_when_no_export_names_tasks(self):
        """With nothing to match against, provisional tasks must stay intact."""

        project = Project()
        project.tasks["task@3@00:00:00.20"] = Task(
            name="task@3@00:00:00.20", interval="00:00:00.20", priority=3,
            programs=["Foo"])
        project.pous["Foo"] = Pou("Foo")
        _assign_tasks(project)
        assert "task@3@00:00:00.20" in project.tasks
        assert project.pous["Foo"].task == "task@3@00:00:00.20"
