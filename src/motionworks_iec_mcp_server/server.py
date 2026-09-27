"""FastMCP server exposing MotionWorks IEC project tools.

Every tool returns a JSON string. Failures are reported as
``{"error": "...", ...}`` rather than raised, so a bad path or an unreadable
export never kills the agent's turn.

Read the module docstrings in :mod:`motionworks_iec_mcp_server.parsers` for the
format specifics — notably that the PLCopen XML export is authoritative for
graphical logic and that the three export forms are complementary rather than
redundant.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastmcp import FastMCP

from motionworks_iec_mcp_server import codegen, fwlib
from motionworks_iec_mcp_server.model import Language, Project, Var
from motionworks_iec_mcp_server.parsers.st import count_statements
from motionworks_iec_mcp_server.parsers.xref import XrefIndex
from motionworks_iec_mcp_server.project import (
    detect,
    load,
    SourceNotFoundError,
    SourceUnrecognizedError,
)

# Bodies larger than this are truncated so a single call cannot flood a context
# window. Callers page with `offset`.
MAX_BODY_LINES = 2000
MAX_LIST_ITEMS = 2000

mcp = FastMCP(
    "MotionWorks IEC MCP Server",
    instructions=(
        "This server provides access to Yaskawa MotionWorks IEC 3 projects from three "
        "export forms: the native project folder, the PLCopen XML export, and the "
        "Extended IEC 61131-2 export. Start with open_project on a project file or "
        "folder; it merges every source it can find and reports where they disagree. "
        "Structured Text is returned as-is; ladder and FBD bodies are returned as "
        "compact per-network text (e.g. 'MC_Power(MC_ServoOn) Axis=TopCutter "
        "Enable=SVON_Cmd') rather than raw XML. The PLCopen XML export is authoritative "
        "for graphical logic; the Extended export is authoritative for plain ST text and "
        "for variable AT % addresses."
    ),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _error(message: str, category: str = "error", **extra: Any) -> str:
    payload: dict[str, Any] = {"error": message, "category": category}
    payload.update(extra)
    return json.dumps(payload, indent=2)


def _dump(value: Any, limit: int = MAX_LIST_ITEMS) -> str:
    if isinstance(value, list) and len(value) > limit:
        value = value[:limit]
    return json.dumps(value, indent=2, default=str)


def _open(path: str) -> Project | str:
    """Load a project or return an error string ready to hand back."""

    try:
        return load(path)
    except SourceNotFoundError as exc:
        return _error(
            f"No such file or directory: {exc}",
            "source_not_found",
            hint="Pass a .xml PLCopen export, an Extended IEC export directory, "
                 "or a native project directory.",
        )
    except SourceUnrecognizedError as exc:
        return _error(
            f"Not a recognised MotionWorks IEC source: {exc}",
            "source_unrecognized",
            hint="Expected a PLCopen XML file (*.xml with a plcopen.org/xc6 "
                 "namespace), an Extended IEC export folder (contains "
                 "Global_Variables.GVB or *.ST/*.GE), or a native project folder "
                 "(contains NODES.LST or *.mwt).",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Failed to load project: {exc}", "parse_error")


def _truncate_body(code: str, offset: int = 0) -> tuple[str, bool, int, int]:
    """Return ``(snippet, truncated, next_offset, total_lines)``."""

    lines = code.splitlines()
    total = len(lines)
    if offset < 0:
        offset = 0
    window = lines[offset : offset + MAX_BODY_LINES]
    truncated = (offset + len(window)) < total
    next_offset = offset + len(window) if truncated else 0
    return "\n".join(window), truncated, next_offset, total


def _matches(text: str, needle: str) -> bool:
    """Case-insensitive substring match that tolerates typos in either direction.

    A one-way ``needle in text`` test fails the most common typo shape — a user
    typing *more* than the real name (``Starterr`` for ``Starter``) produces no
    suggestion at all, which is exactly when a suggestion is most useful. So the
    containment test is symmetric.
    """

    a = (text or "").lower().strip()
    b = (needle or "").lower().strip()
    if not a or not b:
        return False
    return a in b or b in a


def _libraries_for(project: Project) -> list[str]:
    """Firmware libraries this project declares, for scoping the reference load.

    Falls back to the motion libraries, which is what almost every project needs
    and what the shared 19 MB help file covers.
    """

    names = list(project.library_names)
    if not names:
        return list(fwlib.DEFAULT_LIBRARIES)
    # The reference files libraries by folder name; keep any that match, plus the
    # defaults so a block used from an unlisted library is still described.
    merged = list(names)
    for extra in ("PLCopenPlus_v_2_2a", "YMotion", "YCoordinatedMotion"):
        if extra not in merged:
            merged.append(extra)
    return merged


def _reference_pin_map() -> dict[str, set[str]] | None:
    """``{block: {pin names}}`` from the firmware reference, for pin validation.

    An Extended-only project carries no type definitions, so without this the
    validator cannot tell a real pin from an invented one for library blocks.
    """

    catalog, _problems = fwlib.catalog_for()
    if catalog is None:
        return None
    return {
        name: {p.name for p in block.pins}
        for name, block in catalog.blocks.items()
        if block.pins
    }


def _enrich_type_from_reference(payload: dict[str, Any]) -> dict[str, Any]:
    """Add documentation from the firmware reference to a project-sourced type.

    The two disagree in a way worth preserving rather than resolving. For
    ``MC_Direction`` the project export lists five values and the help describes
    four — ``Both`` is accepted by the firmware but undocumented in that help
    version. So the project stays authoritative for *which values exist* and the
    help is authoritative for *what they mean*; values the help does not know are
    kept, and values the help knows but the project does not are listed separately
    instead of being silently merged in.
    """

    catalog, _problems = fwlib.catalog_for()
    if catalog is None:
        return {}
    lib_type = catalog.data_type(payload.get("name", ""))
    if lib_type is None:
        return {}

    out: dict[str, Any] = {"documentedInReference": True}
    if lib_type.description and not payload.get("description"):
        out["description"] = lib_type.description

    project_values = payload.get("enumValues") or []
    if project_values:
        documented = {v["name"]: v for v in lib_type.values}
        enriched = 0
        for value in project_values:
            ref = documented.get(value.get("name", ""))
            if ref and ref.get("description") and not value.get("description"):
                value["description"] = ref["description"]
                enriched += 1
        project_names = {v.get("name") for v in project_values}
        extra = [v for v in lib_type.values if v.get("name") not in project_names]
        if enriched:
            out["valuesEnrichedFromReference"] = enriched
        if extra:
            out["documentedButNotInProject"] = [v["name"] for v in extra]
            out["documentationNote"] = (
                "the firmware reference documents values this project's export does "
                "not list; the export is authoritative for what the firmware accepts"
            )

    project_members = {m.get("name") for m in payload.get("members", [])}
    lib_members = {m.name for m in lib_type.members}
    if lib_members and project_members and lib_members - project_members:
        out["documentedButNotInProject"] = sorted(lib_members - project_members)[:20]
    return out


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


@mcp.tool
def ping() -> str:
    """Health check — verify the server is running."""
    return "pong"


# --------------------------------------------------------------------------
# project level
# --------------------------------------------------------------------------


@mcp.tool
def open_project(path: str) -> str:
    """Open a MotionWorks IEC project, merging every export form present.

    This is the entry point for every other tool. ``path`` may be a PLCopen XML
    file, an Extended IEC 61131-2 export directory, or a native project
    directory — the source is detected, not declared.

    Returns the project summary plus a ``divergences`` list. Read that list: the
    exports are incomplete in different ways (an XML export can omit POUs that
    the Extended export has, or ship them with an empty body), so a divergence is
    often the most important thing about a project.

    Args:
        path: File or directory to open.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    return _dump(result.summary())


@mcp.tool
def load_project(plcopen_xml_path: str) -> str:
    """Parse a PLCopen XML export and return a project summary.

    Kept for parity with the studio5000 connector. ``open_project`` is preferred
    because it also finds the Extended and native sources.

    Args:
        plcopen_xml_path: Path to a PLCopen XML file exported from MotionWorks IEC.
    """
    return open_project(plcopen_xml_path)


@mcp.tool
def get_sources(path: str) -> str:
    """Report which export forms exist for a project and what each contributes.

    Use this when a tool returns less than you expected: it says which sources are
    readable, what each one supplied, and exactly where they disagree.

    Args:
        path: File or directory to inspect.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    per_source: dict[str, dict[str, Any]] = {}
    for pou in project.pous.values():
        stats = per_source.setdefault(
            pou.source, {"pous": 0, "withCode": 0, "languages": {}}
        )
        stats["pous"] += 1
        if (pou.body.code or "").strip() or pou.body.nets:
            stats["withCode"] += 1
        stats["languages"][pou.language] = stats["languages"].get(pou.language, 0) + 1

    return _dump({
        "path": project.path,
        "name": project.name,
        "detected": detect(path),
        "sources": project.sources,
        "sourcePaths": project.source_paths,
        "perSource": per_source,
        "processorType": project.processor_type,
        "counts": {
            "pous": len(project.pous),
            "types": len(project.types),
            "globalVariables": len(project.global_vars),
            "tasks": len(project.tasks),
            "ioPoints": len(project.io_points),
            "translations": len(project.translations),
        },
        "divergences": [d.to_dict() for d in project.divergences],
        "notes": project.notes,
        "tree": project.tree[:120],
    })


# --------------------------------------------------------------------------
# variables
# --------------------------------------------------------------------------


@mcp.tool
def get_tags(
    path: str,
    scope: str = "",
    data_type: str = "",
    address: str = "",
    search: str = "",
) -> str:
    """List project variables (the MotionWorks equivalent of tags).

    Combines global variables (which carry ``AT %`` addresses) with every POU's
    declared interface variables, so this is the full symbol surface of the
    project.

    Args:
        path: Project file or directory.
        scope: Filter by scope, e.g. "VAR_GLOBAL", "VAR_EXTERNAL", "VAR", "VAR_INPUT".
        data_type: Filter by data type, e.g. "BOOL", "LREAL", "AXIS_REF".
        address: Substring filter on the AT address, e.g. "%MX1.7" .
        search: Case-insensitive substring filter on the variable name.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    rows: list[dict[str, Any]] = []
    for var in project.global_vars:
        rows.append({**var.to_dict(), "pou": ""})
    for pou in project.pous.values():
        for var in pou.all_vars():
            rows.append({**var.to_dict(), "pou": pou.name})

    if scope:
        rows = [r for r in rows if r["scope"].upper() == scope.upper()]
    if data_type:
        rows = [r for r in rows if r["type"].upper() == data_type.upper()]
    if address:
        rows = [r for r in rows if _matches(r.get("address", ""), address)]
    if search:
        rows = [r for r in rows if _matches(r["name"], search)]

    return _dump({
        "count": len(rows),
        "variables": rows,
        "scopes": sorted({r["scope"] for r in rows}),
    })


@mcp.tool
def get_tag(path: str, tag_name: str) -> str:
    """Get one variable's full detail plus every place it is used.

    Use this to answer "what is this signal and what drives it?" — it returns the
    declaration (with address and comment) alongside every POU, net and line that
    references it.

    Args:
        path: Project file or directory.
        tag_name: Variable name. A qualified name such as "Products.Sensor.Bit"
            also works.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    matches: list[dict[str, Any]] = []
    for var in project.global_vars:
        if var.name == tag_name or var.name == tag_name.split(".")[0]:
            matches.append({**var.to_dict(), "pou": ""})
    for pou in project.pous.values():
        for var in pou.all_vars():
            if var.name == tag_name or var.name == tag_name.split(".")[0]:
                matches.append({**var.to_dict(), "pou": pou.name})

    uses = XrefIndex(project).uses(tag_name)
    if not matches and not uses:
        return _error(f"Variable {tag_name!r} not found in this project", "tag_not_found")

    type_name = matches[0]["type"] if matches else ""
    members: list[dict[str, Any]] = []
    td = project.types.get(type_name)
    if td is not None:
        members = [m.to_dict() for m in td.members]

    return _dump({
        "name": tag_name,
        "declarations": matches,
        "type": type_name,
        "typeMembers": members,
        "useCount": len(uses),
        "uses": uses[:200],
    })


# --------------------------------------------------------------------------
# types / function blocks
# --------------------------------------------------------------------------


@mcp.tool
def get_types(path: str, search: str = "", kind: str = "", limit: int = 400) -> str:
    """List data types, structs and function blocks available in the project.

    This is the vocabulary an agent must code against — including the Yaskawa
    motion library (``AXIS_REF``, ``Y_ENGAGE_DATA``, ``MC_*``, ``Y_*``,
    ``AxisControl``) when the PLCopen XML export is present, which it is the only
    source that carries.

    Args:
        path: Project file or directory.
        search: Case-insensitive substring filter on the type name.
        kind: Filter by kind: "struct", "enum", "array", "alias" or "fb".
        limit: Maximum number of types to return.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    rows: list[dict[str, Any]] = []
    for td in project.types.values():
        if search and not _matches(td.name, search):
            continue
        if kind and td.kind != kind:
            continue
        rows.append(td.to_dict(members=False))
    rows.sort(key=lambda r: r["name"])
    total = len(rows)
    rows = rows[: max(1, limit)]

    return _dump({
        "count": len(rows),
        "totalMatching": total,
        "kinds": sorted({td.kind for td in project.types.values()}),
        "types": rows,
    })


@mcp.tool
def get_type(path: str, type_name: str = "", search: str = "") -> str:
    """Get a type definition with its members.

    Use ``get_fb_signature`` instead when you want a function block's pin table in
    a form oriented around calling it.

    Args:
        path: Project file or directory.
        type_name: Exact type name. Empty returns every type that matches `search`,
            or all types when `search` is empty too.
        search: Case-insensitive substring filter, used when `type_name` is empty.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    if type_name:
        td = project.types.get(type_name)
        if td is None:
            candidates = [n for n in project.types if n.upper() == type_name.upper()]
            if candidates:
                td = project.types[candidates[0]]
        if td is not None:
            payload = td.to_dict(members=True)
            payload.update(_enrich_type_from_reference(payload))
            return _dump(payload)
        # Fall back to the firmware reference: it documents the library's own
        # types and enums, which no project export defines.
        catalog, problems = fwlib.catalog_for()
        lib_type = catalog.data_type(type_name) if catalog is not None else None
        if lib_type is not None:
            payload = lib_type.to_dict()
            payload["resolvedFrom"] = "firmware library reference"
            return _dump(payload)
        similar = [n for n in project.types if _matches(n, type_name)][:20]
        if catalog is not None:
            similar += [
                h["name"] for h in fwlib.search(catalog, type_name, limit=15)
                if h["kind"] in ("type", "enum")
            ]
        return _error(
            f"Type {type_name!r} not found",
            "type_not_found",
            did_you_mean=similar[:25],
            referenceAvailable=catalog is not None,
            hint=(" / ".join(problems[:2]) if problems else
                  "Use search_library to find library types and enums."),
        )

    if search:
        rows = [td.to_dict(members=True) for n, td in project.types.items() if _matches(n, search)]
        return _dump({"count": len(rows), "types": rows})

    rows = [td.to_dict(members=False) for td in project.types.values()]
    rows.sort(key=lambda r: r["name"])
    return _dump({"count": len(rows), "types": rows})


@mcp.tool
def get_udts(path: str) -> str:
    """List user-defined types only (project-defined, excluding the vendor library).

    Args:
        path: Project file or directory.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result
    names = sorted(n for n, td in project.types.items() if not td.is_library)
    all_names = sorted(project.types)
    return _dump({
        "count": len(names),
        "udts": names,
        "libraryTypeCount": len(all_names) - len(names),
    })


@mcp.tool
def get_udt(path: str, udt_name: str = "") -> str:
    """Get a user-defined type definition with its members.

    Args:
        path: Project file or directory.
        udt_name: Type name. Empty returns all project-defined types.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result
    if udt_name:
        return get_type(path, udt_name)
    rows = [
        td.to_dict(members=True)
        for td in project.types.values()
        if not td.is_library
    ]
    return _dump({"count": len(rows), "udts": rows})


@mcp.tool
def get_fb_signature(path: str, fb_name: str) -> str:
    """Get a function block's interface — for calling or instancing it correctly.

    Combines two authorities, because each knows something the other does not:

    * the **project** knows the exact pin list its firmware build compiled against
      (from ``.DIT`` metadata) or what the code actually passes;
    * the **firmware library reference** knows data types, defaults, per-pin
      meaning, which pins this firmware leaves unimplemented, the block's purpose,
      and a usage example.

    Pins the project does not mention are still listed, so a block can be called
    with parameters it has never been called with here. ``unsupportedPins`` names
    parameters that exist on the block but do nothing on this firmware.

    Args:
        path: Project file or directory.
        fb_name: Function block name, e.g. "MC_Power", "Y_CamIn", "AxisControl".
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    project_signature = codegen.fb_signature(project, fb_name)
    catalog, problems = fwlib.catalog_for(libraries=_libraries_for(project))
    block = catalog.block(fb_name) if catalog is not None else None

    if block is not None:
        return _dump(fwlib.merge_signature(project_signature, block))

    if project_signature is not None:
        return _dump(project_signature)

    available = sorted(codegen.fb_usage_catalog(project))
    similar = [n for n in available if _matches(n, fb_name)][:25]
    declared = [n for n in project.types if _matches(n, fb_name)][:25]
    hinted = []
    if catalog is not None:
        hinted = [h["name"] for h in fwlib.search(catalog, fb_name, limit=15)]
    return _error(
        f"Function block {fb_name!r} not found in this project or the firmware reference",
        "fb_not_found",
        did_you_mean=(similar or declared or hinted),
        referenceAvailable=catalog is not None,
        hint=(" / ".join(problems[:2]) if problems else
              "Use list_library_blocks to see every block the firmware provides."),
    )


@mcp.tool
def list_library_blocks(
    search: str = "",
    library: str = "",
    limit: int = 200,
) -> str:
    """List the function blocks the MotionWorks firmware library provides.

    This is device documentation, not project state: it works without opening a
    project, and it includes blocks this project never uses. Use it to find out
    what is available before writing code.

    Args:
        search: Case-insensitive substring filter on the block name.
        library: Filter by library, e.g. "YMotion" or "PLCopen".
        limit: Maximum number of blocks to return.
    """
    catalog, problems = fwlib.catalog_for()
    if catalog is None:
        return _error("The firmware library reference is unavailable",
                      "reference_unavailable", reasons=problems,
                      hint="Run get_library_reference_status for how to fix this.")

    rows = []
    for block in catalog.blocks.values():
        if search and not _matches(block.name, search):
            continue
        if library and library.lower() not in (block.library or "").lower():
            continue
        rows.append({
            "name": block.name,
            "library": block.library,
            "description": block.description,
            "pinCount": len(block.pins),
            "unsupportedPins": [p.name for p in block.pins if not p.supported],
        })
    rows.sort(key=lambda r: r["name"])
    total = len(rows)
    return _dump({
        "count": len(rows[:max(1, limit)]),
        "totalMatching": total,
        "libraries": catalog.libraries,
        "libraryCount": len(catalog.blocks),
        "blocks": rows[: max(1, limit)],
        "usageHint": "call get_fb_signature for one block's full parameter list",
    })


@mcp.tool
def search_library(query: str, limit: int = 40) -> str:
    """Search the firmware library by block name, pin name, type or description.

    Answers "which block do I use for X?" — searching names, formal parameters,
    descriptions, notes and example code, plus enumerated types and their values.

    Args:
        query: Text to look for, e.g. "torque", "cam in", "BufferMode", "alarm".
        limit: Maximum number of matches.
    """
    catalog, problems = fwlib.catalog_for()
    if catalog is None:
        return _error("The firmware library reference is unavailable",
                      "reference_unavailable", reasons=problems,
                      hint="Run get_library_reference_status for how to fix this.")
    hits = fwlib.search(catalog, query, limit=max(1, limit))
    return _dump({
        "query": query,
        "matchCount": len(hits),
        "matches": hits,
        "reference": {"version": catalog.version, "blocks": len(catalog.blocks)},
    })


@mcp.tool
def get_library_reference_status() -> str:
    """Report whether the firmware library reference is available, and how to enable it.

    The reference is the vendor's own help, installed with MotionWorks and
    decompiled locally. It is what makes the motion library's parameters, types and
    semantics knowable. If this reports it is unavailable, that is the reason
    library signatures fall back to observed usage.
    """
    fw_lib = fwlib.find_install()
    payload: dict[str, Any] = {
        "available": False,
        "installPath": str(fw_lib) if fw_lib else "",
        "cacheDir": str(fwlib.default_cache_dir()),
        "decompiler": "hh.exe" if fwlib.have_decompiler() else "not available",
    }
    catalog, problems = fwlib.catalog_for()
    if catalog is not None:
        payload["available"] = True
        payload["catalog"] = catalog.summary()
        payload["howItWorks"] = (
            "MotionWorks ships each library's reference as compiled help (.chm). "
            "Pages are decompiled with hh.exe into the cache directory, parsed once, "
            "and the parse is cached. Nothing in the repository contains vendor text."
        )
    else:
        payload["reasons"] = problems
        payload["howToEnable"] = [
            "Install MotionWorks IEC (the reference ships at "
            "C:\\ProgramData\\Yaskawa\\MotionWorks IEC 3 Pro\\<version>\\plc\\FW_LIB).",
            "Or set MOTIONWORKS_FWLIB to an FW_LIB folder.",
            "Or set MOTIONWORKS_FWLIB to a folder of already-extracted .htm pages "
            "(no hh.exe needed).",
            "Set MOTIONWORKS_FWLIB_CACHE to relocate the extraction cache.",
        ]
    return _dump(payload)


@mcp.tool
def list_fb_catalog(path: str, search: str = "") -> str:
    """List every function block the project uses, with the parameters it passes.

    This is the practical coding vocabulary: the blocks that are actually
    available in this machine and the formal parameter names they are actually
    called with. The vendor library is not exported as type definitions, so this
    is the only complete view of it for a given project.

    Args:
        path: Project file or directory.
        search: Case-insensitive substring filter on the block name.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    catalog = codegen.fb_usage_catalog(project)
    rows = [v for k, v in catalog.items() if not search or _matches(k, search)]
    rows.sort(key=lambda r: (-r["useCount"], r["name"]))
    library = [r for r in rows if not r["declaredType"]]
    return _dump({
        "count": len(rows),
        "libraryBlockCount": len(library),
        "declaredBlockCount": len(rows) - len(library),
        "note": (
            "Blocks with declaredType=false come from the firmware library and are "
            "not defined in the export; their parameter lists are observed from "
            "call sites."
        ),
        "blocks": rows,
    })


# --------------------------------------------------------------------------
# POUs
# --------------------------------------------------------------------------


@mcp.tool
def get_pous(
    path: str,
    language: str = "",
    task: str = "",
    search: str = "",
) -> str:
    """List programs and function blocks with language, task and size.

    Args:
        path: Project file or directory.
        language: Filter by body language: "ST", "LD", "FBD".
        task: Filter by the task the program runs under, e.g. "FastTsk".
        search: Case-insensitive substring filter on the POU name.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    rows = []
    for pou in project.pous.values():
        if language and pou.language.upper() != language.upper():
            continue
        if task and (pou.task or "").upper() != task.upper():
            continue
        if search and not _matches(pou.name, search):
            continue
        rows.append(pou.to_dict(include_members=False))
    rows.sort(key=lambda r: r["name"])

    return _dump({
        "count": len(rows),
        "pous": rows,
        "languages": sorted({p.language for p in project.pous.values()}),
        "tasks": sorted({p.task for p in project.pous.values() if p.task}),
    })


@mcp.tool
def get_routines(path: str, program: str = "") -> str:
    """List POUs (alias for ``get_pous``, for familiarity with studio5000).

    Args:
        path: Project file or directory.
        program: Filter by POU name.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result
    rows = [p.to_dict() for p in project.pous.values() if not program or p.name == program]
    return _dump({"count": len(rows), "pous": rows})


@mcp.tool
def get_pou(
    path: str,
    pou_name: str,
    offset: int = 0,
    include_interface: bool = True,
    include_outputs: bool = True,
) -> str:
    """Get one POU: its interface, its body, and every network it contains.

    This is the workhorse tool. Structured Text is returned as the original
    source, tab alignment and inline comments intact. Ladder and FBD bodies are
    returned as one compact text line per network plus a structured ``nets`` array
    naming each block, its instance and each formal parameter's source — the
    equivalent of NeutralText for Studio 5000, and far smaller than the raw XML.

    Long bodies are paged: check ``body.truncated`` and pass ``offset`` to
    continue.

    Args:
        path: Project file or directory.
        pou_name: POU name exactly as reported by `get_pous`.
        offset: Line offset into the body, for paging long POUs.
        include_interface: Include the declared variables for each scope.
        include_outputs: For graphical bodies, include each block's output pins.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    pou = project.pous.get(pou_name)
    if pou is None:
        similar = [n for n in project.pous if _matches(n, pou_name)][:20]
        return _error(
            f"POU {pou_name!r} not found in this project",
            "pou_not_found",
            available=sorted(project.pous),
            did_you_mean=similar,
        )

    payload: dict[str, Any] = {
        "name": pou.name,
        "pouType": pou.pou_type,
        "language": pou.language,
        "task": pou.task,
        "description": pou.description,
        "source": pou.source,
    }
    if pou.pou_id is not None:
        payload["id"] = pou.pou_id

    if include_interface:
        payload["interface"] = pou.interface_dict()

    code = pou.body.code or ""
    snippet, truncated, next_offset, total = _truncate_body(code, offset)
    body: dict[str, Any] = {
        "language": pou.body.language,
        "code": snippet,
    }
    # Which export this body actually came from. Distinct from the POU's own
    # `source`, which names the record that supplied its metadata and interface —
    # in a merged project those can be different files.
    if pou.body.source:
        body["source"] = pou.body.source
    if pou.body.statements:
        body["statementCount"] = pou.body.statements
    if pou.body.nets:
        body["networkCount"] = len(pou.body.nets)
        body["nets"] = [n.to_dict(include_text=True) if include_outputs else n.to_dict() for n in pou.body.nets]
    if pou.body.unsupported:
        body["unsupported"] = pou.body.unsupported
    if truncated:
        body["truncated"] = True
        body["nextOffset"] = next_offset
        body["totalLines"] = total
    payload["body"] = body

    return _dump(payload)


@mcp.tool
def get_routine(path: str, program: str, routine_name: str = "") -> str:
    """Get a POU's body (alias for ``get_pou``, for familiarity with studio5000).

    Args:
        path: Project file or directory.
        program: POU name.
        routine_name: Ignored; present so the studio5000 calling convention works.
    """
    return get_pou(path, program)


# --------------------------------------------------------------------------
# tasks, I/O, motion
# --------------------------------------------------------------------------


@mcp.tool
def get_tasks(path: str) -> str:
    """List controller tasks with timing and the programs each one runs.

    Task assignment decides execution order and jitter, so check this before
    assuming code in two POUs runs in a particular sequence.

    Args:
        path: Project file or directory.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    tasks = []
    for task in project.tasks.values():
        entry = task.to_dict()
        # Name the language of each program so the scheduling picture is complete.
        entry["programLanguages"] = {
            p: project.pous[p].language for p in task.programs if p in project.pous
        }
        entry["programsMissingFromExports"] = [
            p for p in task.programs if p not in project.pous
        ]
        tasks.append(entry)
    tasks.sort(key=lambda t: (t.get("priority") if t.get("priority") is not None else 99, t["name"]))

    return _dump({
        "count": len(tasks),
        "tasks": tasks,
        "processorType": project.processor_type,
        "resource": project.resource_type,
    })


@mcp.tool
def get_io_config(path: str) -> str:
    """List the controller and network I/O configuration.

    Each entry is a named I/O group mapped to an address range and a driver, which
    is how the ``AT %I``/``%Q`` variables in the global variable table get their
    physical meaning.

    Args:
        path: Project file or directory.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    rows = [p.to_dict() for p in project.io_points]
    return _dump({
        "count": len(rows),
        "io": rows,
        "drivers": sorted({r["driver"] for r in rows if r["driver"]}),
        "note": (
            "No I/O configuration found. It comes from the Extended IEC export "
            "(IOCONFIGURATION.EIO)."
            if not rows else ""
        ),
    })


@mcp.tool
def get_motion_config(path: str) -> str:
    """Summarise the motion setup: axes, their amplifier bindings and network nodes.

    MotionWorks binds an axis to hardware through ``<var>.AxisNum`` assignments in
    startup code (e.g. ``TopCutter.AxisNum := UINT#1;``) and the servo groups in
    the I/O configuration (``SGD7S Network #1 Node #1``). This tool joins those two
    so "which servo is axis 1?" is answerable.

    Args:
        path: Project file or directory.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    # AxisNum bindings first: `<var>.AxisNum := <literal>;`. These are the
    # authoritative axis declarations, and they catch axes whose type is not
    # literally AXIS_REF (projects name them AXIS3, AXIS4, ...).
    bindings: list[dict[str, Any]] = []
    axis_names: set[str] = set()
    for pou in project.pous.values():
        code = pou.body.code or ""
        for m in re.finditer(
            r"\b(?P<axis>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*AxisNum\s*:=\s*(?P<val>[^;]+);",
            code,
        ):
            axis = m.group("axis")
            axis_names.add(axis)
            bindings.append({
                "axis": axis,
                "axisNum": m.group("val").strip(),
                "pou": pou.name,
            })

    # Every variable that is an axis reference: by type, or by being bound above.
    axis_vars: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    axis_types = {"AXIS_REF", "AXES_GROUP_REF", "MC_COORD_REF"}
    for var in list(project.global_vars) + [
        v for pou in project.pous.values() for v in pou.all_vars()
    ]:
        is_axis = var.type_name.upper() in axis_types or var.name in axis_names
        if not is_axis:
            continue
        # De-duplicate by name: the same axis is declared in several POUs, and a
        # repeated axis would make the axis list look larger than it is.
        key = (var.name, var.scope)
        if key in seen or any(a["name"] == var.name for a in axis_vars):
            continue
        seen.add(key)
        entry = var.to_dict()
        if var.name in axis_names:
            entry["hasAxisNumBinding"] = True
        axis_vars.append(entry)

    # Servo / network groups from the I/O configuration.
    groups: list[dict[str, Any]] = []
    for point in project.io_points:
        group = point.group or ""
        if any(k in group.upper() for k in ("SGD", "SERVO", "NETWORK", "AXIS", "MP", "EIP")):
            groups.append(point.to_dict())

    motion_types = sorted(
        n for n in project.types
        if n.startswith(("Y_", "MC_")) or n in axis_types
    )

    return _dump({
        "axisCount": len({b["axis"] for b in bindings}) or len(axis_vars),
        "axisBindings": bindings,
        "axisReferences": axis_vars,
        "ioGroups": groups,
        "motionTypesAvailable": motion_types[:300],
        "motionTypeCount": len(motion_types),
        "ioPointCount": len(project.io_points),
        "processorType": project.processor_type,
        "note": (
            "Axis-to-servo mapping comes from <axis>.AxisNum assignments and the "
            "servo groups in the I/O configuration. The firmware motion library "
            "is not exported as type definitions; use list_fb_catalog for the "
            "blocks this project actually calls."
        ),
    })


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


@mcp.tool
def search_logic(path: str, pattern: str, limit: int = 400) -> str:
    """Search for a tag, block or regex across every POU and network.

    Answers "where is this used?" and "which routines call this block?". Matches
    symbol-table entries (declarations, block types, instances, pins) first, then
    raw Structured Text lines, so both a tag name and a regex work.

    Note the pattern is an ordinary regex, so ``MC_`` matches every ``MC_*``
    block reference (the underscore is literal, and ``MC_\\d+`` would require
    digits after it and so match nothing).

    Args:
        path: Project file or directory.
        pattern: Tag name, block name, or regex pattern.
        limit: Maximum number of matches to return.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    try:
        hits = XrefIndex(project).search(pattern, limit=max(1, limit))
    except ValueError as exc:
        return _error(str(exc), "bad_pattern")

    by_kind: dict[str, int] = {}
    for hit in hits:
        by_kind[hit["kind"]] = by_kind.get(hit["kind"], 0) + 1

    return _dump({
        "pattern": pattern,
        "matchCount": len(hits),
        "byKind": by_kind,
        "matches": hits,
    })


@mcp.tool
def get_code_conventions(path: str) -> str:
    """Report the coding conventions this project already follows.

    Call this **before generating code**. Conventions are written down nowhere —
    they are visible only in the existing symbols — and code that compiles but
    breaks them reads as foreign in review. Each rule carries the evidence behind
    it and a confidence, so a weak signal is not mistaken for a house standard.

    Covers: variable name prefixes by type (``x``→BOOL, ``r``→LREAL, ``udi``→UDINT),
    function-block instance naming, established signal families, documentation
    habits, task structure and the languages in use.

    Args:
        path: Project file or directory.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    try:
        analysis = codegen.conventions.analyse(project)
    except Exception as exc:
        return _error(f"Failed to analyse conventions: {exc}", "analysis_error")

    if not analysis.get("symbolCount"):
        analysis["note"] = (
            "No declared variables were readable, so no conventions could be "
            "observed. Open a project with code (the Extended IEC export carries "
            "declarations) or read the conventions from the reference instead."
        )
    analysis["pouCount"] = len(project.pous)
    return _dump(analysis)


# --------------------------------------------------------------------------
# code generation support
# --------------------------------------------------------------------------


@mcp.tool
def validate_pou(
    path: str,
    code: str,
    declared_vars: str = "",
    pou_name: str = "",
) -> str:
    """Validate agent-written IEC code against the project's real symbols.

    Call this on generated code *before* handing it to a user. It catches the
    mistakes an LLM actually makes: tags that do not exist in the project, types
    that are not defined, function blocks that are not in the library, and formal
    parameter names that the target block does not have.

    Args:
        path: Project file or directory.
        code: The Structured Text body to check (no declaration blocks).
        declared_vars: Any VAR blocks the code relies on, so locally declared
            symbols are not reported as undeclared.
        pou_name: Optional name, used only for labelling the result.
    """
    result = _open(path)
    if isinstance(result, str):
        return result
    project: Project = result

    try:
        outcome = codegen.validate(
            project, code,
            declared_vars=declared_vars,
            pou_name=pou_name,
            reference_pins=_reference_pin_map(),
        )
    except Exception as exc:
        return _error(f"Validation failed: {exc}", "validation_error")

    payload = outcome.to_dict()
    payload["pouName"] = pou_name
    payload["statementCount"] = count_statements(code)
    return _dump(payload)


@mcp.tool
def render_pou_source(
    name: str,
    pou_type: str = "program",
    language: str = "ST",
    declaration: str = "",
    code: str = "",
    description: str = "",
) -> str:
    """Emit a POU in the shape MotionWorks' Extended IEC 61131-2 export uses.

    Produces the ``(*@PROPERTIES_EX@ ... *)`` header, the PROGRAM/FUNCTION_BLOCK
    line, the declaration blocks and the ``(*@KEY@: WORKSHEET ... *)`` body region —
    the same structure the IDE writes when exporting a POU as text.

    Whether the IDE *imports* this shape is not verified: that format is
    undocumented in the installed help, whose documented import path is PLCopen XML
    (``File`` → ``Import`` → "Import PLCopen xml file"). So either create a POU in
    the IDE and paste the code, or wrap the result in PLCopen XML for the documented
    route.

    Args:
        name: POU name.
        pou_type: "program", "function_block" or "function".
        language: Body language — "ST", "LD" or "FBD".
        declaration: The VAR blocks, without the closing END_* keyword.
        code: The Structured Text body.
        description: Free text stored in the POU's DESCRIPTION region.
    """
    try:
        text = codegen.render_pou_source(
            name=name,
            pou_type=pou_type,
            language=language,
            declaration=declaration,
            code=code,
            description=description,
        )
    except Exception as exc:
        return _error(f"Failed to render POU: {exc}", "render_error")

    return _dump({
        "name": name,
        "pouType": pou_type,
        "language": language,
        "suggestedFileName": f"{name}.{language.upper()}",
        "lineCount": len(text.splitlines()),
        "source": text,
    })


@mcp.tool
def render_type_source(
    name: str,
    members: list[dict] | None = None,
    comment: str = "",
) -> str:
    """Emit a ``TYPE ... END_TYPE`` struct definition in MotionWorks' text shape.

    Same caveat as ``render_pou_source``: the structure matches what the IDE
    exports, but the documented import path is PLCopen XML.

    Args:
        name: Type name.
        members: List of objects like ``{"name": "PartLength", "type": "LREAL",
            "comment": "mm"}``.
        comment: Optional description stored in the DESCRIPTION region.
    """
    try:
        text = codegen.render_type_source(name, members or [], comment=comment)
    except Exception as exc:
        return _error(f"Failed to render type: {exc}", "render_error")

    return _dump({
        "name": name,
        "memberCount": len(members or []),
        "suggestedFileName": f"{name}.IEC",
        "source": text,
    })


@mcp.tool
def list_languages() -> str:
    """Report which IEC body languages this server can render, and how well.

    Useful context for interpreting results: graphical bodies from the PLCopen XML
    export are complete, whereas the same logic recovered from an Extended export's
    ``.GE`` file is partial by design (see the server README).
    """
    return _dump({
        "languages": [
            {"name": "ST", "supported": True, "source": "PLCopen XML (full) and Extended IEC (full)"},
            {"name": "LD", "supported": True, "source": "PLCopen XML (full); Extended IEC .GE (partial)"},
            {"name": "FBD", "supported": True, "source": "PLCopen XML (full); Extended IEC .GE (partial)"},
            {"name": "SFC", "supported": False, "source": "not observed in any export"},
            {"name": "IL", "supported": False, "source": "not observed in any export"},
        ],
        "guidance": (
            "For complete graphical logic, open the PLCopen XML export. The Extended "
            "export is preferred for Structured Text and for variable AT % addresses, "
            "and it is the only source of I/O configuration and task names."
        ),
    })
