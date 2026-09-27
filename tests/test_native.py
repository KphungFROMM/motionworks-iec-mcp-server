"""Tests for the native project parser.

The native project cannot supply code — ``src.st1`` is a compound binary. What it
does supply is the authoritative POU/task map from
``eCLRPouDependencies.dat``, which these tests pin down.
"""

from __future__ import annotations

from pathlib import Path

from motionworks_iec_mcp_server.model import Project
from motionworks_iec_mcp_server.parsers import native


def _parse(root: Path) -> Project:
    project = Project()
    return native.parse(root, project)


class TestDetection:
    def test_detects_a_native_project(self, native_project: Path):
        assert native.is_native_project(native_project)

    def test_rejects_an_empty_folder(self, tmp_path: Path):
        assert not native.is_native_project(tmp_path)


class TestProjectTree:
    def test_nodes_lst_rows_are_parsed(self, native_project: Path):
        project = _parse(native_project)
        kinds = [e["kind"] for e in project.tree if e.get("record") != "poudep"]
        assert "CONFIGURATION" in kinds
        assert "RESOURCE" in kinds
        assert kinds.count("PROGRAM") == 2

    def test_processor_type_comes_from_the_resource_row(self, native_project: Path):
        project = _parse(native_project)
        assert project.processor_type == "MP3300iec"

    def test_row_fields_are_split_correctly(self, native_project: Path):
        project = _parse(native_project)
        prog = next(
            e for e in project.tree
            if e.get("kind") == "PROGRAM" and e.get("name") == "Starter"
        )
        assert prog["subName"] == "Starter"
        assert prog["depth"] == 6


class TestPouIndex:
    def test_schema_banner_is_not_a_row(self, native_project: Path):
        """Regression: 'PouDep.Cache,schema 1' must not enter the tree."""

        project = _parse(native_project)
        assert all(e.get("name") != "PouDep.Cache" for e in project.tree)

    def test_kinds_are_mapped(self, native_project: Path):
        project = _parse(native_project)
        declared = native.declared_pous(project)
        assert declared["Helper"]["kind"] == "function_block"
        assert declared["Starter"]["kind"] == "program"
        assert "FastTsk" not in declared          # tasks are not POUs

    def test_ids_are_preserved(self, native_project: Path):
        project = _parse(native_project)
        declared = native.declared_pous(project)
        assert declared["Helper"]["id"] == 0
        assert declared["Filler"]["id"] == 2


class TestTaskMembership:
    def test_tasks_are_recovered_from_the_dependency_index(self, native_project: Path):
        """A ``TA`` row's dependencies are its programs — the whole point of the file."""

        project = _parse(native_project)
        assert project.tasks["FastTsk"].programs == ["Starter", "Filler"]

    def test_task_timing_comes_from_the_set_file(self, native_project: Path):
        project = _parse(native_project)
        task = project.tasks["FastTsk"]
        assert task.task_type == "CYCLIC"
        assert task.interval == "T#4ms"
        assert task.priority == 0
        assert task.watchdog == "4"

    def test_no_tasks_when_the_index_is_absent(self, tmp_path: Path):
        root = tmp_path / "Bare"
        (root / "C").mkdir(parents=True)
        (root / "POE").mkdir(parents=True)
        (root / "NODES.LST").write_text("CONFIGURATION\t2\tConfiguration\t\teCLR\n", encoding="utf-8")
        project = _parse(root)
        assert project.tasks == {}


class TestBinarySafety:
    def test_compound_binary_files_are_never_parsed_as_code(self, native_project: Path):
        project = _parse(native_project)
        assert project.pous == {}

    def test_project_info_is_read(self, native_project: Path):
        project = _parse(native_project)
        assert "2026" in project.creation
