"""The dependency surface is a feature, so it is tested.

This server runs next to industrial machinery, and it parses formats nobody
documents. Every third-party package is something that can break, go unmaintained,
or pull in a transitive mess, so the parser layer is deliberately standard library
only. `fastmcp` is the single runtime dependency, and it is needed precisely because
the whole point is to speak MCP.

These tests fail if that drifts — an undeclared import, or a declared dependency that
is not actually used.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "motionworks_iec_mcp_server"
PYPROJECT = ROOT / "pyproject.toml"


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _declared_dependencies() -> set[str]:
    """Runtime dependency names, normalised the way import names look."""

    declared = _pyproject()["project"].get("dependencies", [])
    return {name.split("==")[0].split(">=")[0].split("[")[0].strip().lower()
            for name in declared}


def _third_party_imports() -> dict[str, set[str]]:
    """``{module name: {imported top-level packages}}`` for everything in ``src/``."""

    stdlib = set(sys.stdlib_module_names)
    internal_root = "motionworks_iec_mcp_server"
    found: dict[str, set[str]] = {}

    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import (level > 0) is internal by definition.
                candidates = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for candidate in candidates:
                top = candidate.split(".")[0]
                if not top or top in stdlib or top == internal_root:
                    continue
                names.add(top)
        if names:
            found[str(path.relative_to(SRC))] = names
    return found


def test_no_undeclared_third_party_imports():
    declared = _declared_dependencies()
    used: set[str] = set()
    for names in _third_party_imports().values():
        used |= names
    undeclared = used - declared
    assert not undeclared, (
        f"src/ imports {sorted(undeclared)} but pyproject.toml declares only "
        f"{sorted(declared)}. Add the dependency (and say why) or remove the import."
    )


def test_declared_dependencies_are_actually_used():
    declared = _declared_dependencies()
    used: set[str] = set()
    for names in _third_party_imports().values():
        used |= names
    assert declared <= used, (
        f"pyproject.toml declares {sorted(declared - used)}, which src/ never imports."
    )


def test_only_fastmcp_is_third_party():
    """The concrete promise, stated once so a change to it is a deliberate act."""

    used: set[str] = set()
    for names in _third_party_imports().values():
        used |= names
    assert used == {"fastmcp"}


def test_parsers_are_standard_library_only():
    """The parser layer has the tightest budget: it is the part that reads
    undocumented vendor formats, and it should not also depend on a library."""

    offenders = {
        name: sorted(pkgs)
        for name, pkgs in _third_party_imports().items()
        if name.startswith("parsers/") or name in ("fwlib.py", "model.py", "util.py")
    }
    assert not offenders, f"stdlib-only modules import third-party code: {offenders}"


def test_requires_python_matches_the_code():
    """`X | Y` annotations and `dict[str, str]` are used, so 3.10 is the floor."""

    requires = _pyproject()["project"]["requires-python"]
    assert requires == ">=3.10"


@pytest.mark.parametrize("name", ["README.md", "FORMATS.md", "WORKFLOW.md", "LICENSE"])
def test_packaging_files_are_present(name):
    assert (ROOT / name).is_file(), f"{name} is missing from the repository root"


def test_entry_point_targets_a_real_function():
    scripts = _pyproject()["project"]["scripts"]
    target = scripts["motionworks-iec-mcp-server"]
    module_name, _, function_name = target.partition(":")
    relative = Path(*module_name.split(".")).with_suffix(".py")
    module_path = SRC / relative.relative_to("motionworks_iec_mcp_server")
    assert module_path.is_file(), f"entry point module {module_name} does not exist"

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert function_name in functions, (
        f"entry point calls {function_name}() but {module_path.name} defines "
        f"{sorted(functions)}"
    )
