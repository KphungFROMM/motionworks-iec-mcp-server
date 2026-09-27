"""Parse a native MotionWorks IEC project directory.

The native project (``<Project>.mwt`` plus its ``C/``, ``POE/``, ``DT/``,
``HW/`` and ``LIB/`` folders) stores implementation code in a proprietary
compound binary form — ``POE/<Pou>/src.st1`` and ``tmp.sto`` are *not* readable
text. It therefore cannot supply code.

What it *can* supply is the project's authoritative shape, and this module reads
exactly that, from four plain-text files:

``NODES.LST``
    The configuration/resource/task/program tree, tab separated, one level
    column.
``eCLRPouDependencies.dat``
    The POU index: every program, function block and task with a stable id, its
    kind (``PG``/``FB``/``TA``) and its dependencies. This file alone answers
    "which POUs exist and which task runs each one" — the single most useful
    thing the native project offers.
``OCIRES.INI``
    Processor identity (``ProcessorType``), e.g. ``MP2600iec``.
``<Task>.SET`` / ``Resource.set``
    IEC task configuration (``TYPE``, ``INTERVAL``, ``PRIORITY``, ``WATCHDOG``)
    and the resource's comms setting.

``eCLRPouInfo.pil`` is compressed and deliberately skipped.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ..model import Language, Project, Scope, SourceKind, Task, TypeDef, Var
from ..util import read_text

SOURCE = SourceKind.NATIVE.value

_KIND = {
    "PG": "program",
    "FB": "function_block",
    "FC": "function",
    "TA": "task",
    "CF": "configuration",
    "RS": "resource",
}

_TASK_SET_RE = re.compile(
    r"TASK\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<body>.*?)\)\s*;",
    re.DOTALL | re.IGNORECASE,
)
# A value ends at a comma, a closing paren, or a line break. Newline has to be a
# terminator: the ``TASK X (...);`` body contains no inner ``)``, so without it
# the first key's value swallows every key that follows.
_KV_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:=\s*(?P<val>[^,)\r\n]*)")
_PROCTYPE_RE = re.compile(r"ProcessorType\s*=\s*(?P<v>[A-Za-z0-9_]+)")

# --- library interface recovery -------------------------------------------------
# Every project's ``C/Configuration/R/Resource/`` holds one ``.DIT`` per POU *and
# per firmware function block it uses*. Those carry the full formal parameter list
# with scope and declaration order — the only place the vendor motion library's
# interface is written down. ``TYLLIST.TYP`` maps the registered type ids, and the
# small builtin ids are learned from the project's own declared variables.
_DIT_TYPE_TAG_RE = re.compile(r"T:\s*(?P<kind>[A-Z_]+)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
_DIT_ROW_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"\t(?P<slot>\d+)"
    r"\t(?P<scope>VAR_[A-Z_]+)"
    r"\t(?P<type>@TYP:\d+|[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_TYL_ROW_RE = re.compile(
    r"^(?P<row>\d+)\s+\d+\t(?P<ns>[^\t]*)\t(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"\t(?P<id>\d+)\t",
    re.MULTILINE,
)
_TYP_ORD_RE = re.compile(r"@TYP:(\d+)")

# Builtin (small) type ids, verified by correlating every project's .DIT rows with
# its own declared variables on MotionWorks IEC 3 Pro 3.7.5.1: id 1 resolved to
# BOOL with 171 independent confirmations, 3 to INT with 44, 4 to DINT with 10,
# 7 to UINT with 11, 11 to LREAL with 5, with no contradictions.
#
# This is a *fallback* only. When a project's own code can be read, the table is
# learned from that project and takes precedence, so a firmware change that
# renumbers the builtins corrects itself rather than silently lying.
#
# Ids >= 1024 are registered types and are per-file local, so they are never
# guessed — see parse_library_interfaces().
_BUILTIN_TYPE_IDS: dict[int, str] = {
    1: "BOOL",
    3: "INT",
    4: "DINT",
    7: "UINT",
    11: "LREAL",
}

# Scope ordering for a readable, declaration-ordered pin list.
_DIT_SCOPE_ORDER = (
    Scope.VAR_IN_OUT.value,
    Scope.VAR_INPUT.value,
    Scope.VAR_OUTPUT.value,
    Scope.VAR.value,
)


def is_native_project(path: str | Path) -> bool:
    """Does this directory look like a native MotionWorks IEC project?"""

    p = Path(path)
    if not p.is_dir():
        return False
    if (p / "NODES.LST").exists() or (p / "PROJECT.INF").exists():
        return True
    if list(p.glob("*.mwt")):
        return True
    # A project root may be named after the .mwt and hold only C/ + POE/.
    return (p / "POE").is_dir() and (p / "C").is_dir()


def parse(root: str | Path, project: Project) -> Project:
    """Populate ``project`` from a native project directory."""

    root = Path(root)
    if SOURCE not in project.sources:
        project.sources.append(SOURCE)
    project.source_paths[SOURCE] = str(root)
    if not project.name:
        project.name = root.name

    _parse_nodes(root, project)
    _parse_pou_dependencies(root, project)
    _parse_resource_ini(root, project)
    _parse_task_sets(root, project)
    _parse_project_info(root, project)
    _parse_library_info(root, project)
    # Runs last: it needs the declared-POU index to tell library blocks from
    # project-defined ones.
    parse_library_interfaces(root, project)
    return project


def _parse_library_info(root: Path, project: Project) -> None:
    """Read ``eClrLibInfo.txt``, which names the firmware libraries a project used.

    Entries are absolute DLL paths, e.g. ``…\\FW_LIB\\YMotion\\YMotion.DLL``. The
    library name is the folder above the DLL, which is also how the installed help
    is filed — so this is what ties a project to its reference documentation
    instead of loading every library's help.
    """

    for f in root.rglob("eClrLibInfo.txt"):
        try:
            text = read_text(f)
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"[\\/]", line)
            if len(parts) >= 2:
                name = parts[-2]
                if name and name not in project.library_names:
                    project.library_names.append(name)


# --------------------------------------------------------------------------
# Library interfaces from .DIT + TYLLIST.TYP
# --------------------------------------------------------------------------


def parse_library_interfaces(root: Path, project: Project) -> int:
    """Recover real function-block interfaces from ``.DIT`` files.

    This is what makes the motion library usable rather than merely nameable. A
    ``.DIT`` gives each block's formal parameters in declaration order with their
    scope, but types as ``@TYP:<n>`` ids. Those ids are resolved from two places:

    1. ``TYLLIST.TYP`` maps the registered ids (>= 1024) to type names.
    2. The small builtin ids are **learned from the project itself**: each ``.DIT``
       row names a variable, and the parsed POUs and global table say what type
       that variable is, so joining the two yields ``@TYP:1 -> BOOL``,
       ``@TYP:1053 -> AXIS_REF``, and so on. That join is the trick that makes this
       work with no hardcoded table and no vendor documentation.

    An id that resolves to two different types is left unresolved and reported
    through :func:`~..project.Project.add_divergence`, rather than picking one.
    """

    dit_files = sorted(root.rglob("*.DIT"))
    if not dit_files:
        return 0

    registered = _registered_type_ids(root)
    known = _known_symbol_types(project)

    # Read every DIT once: the rows feed both the ordinal learning pass and the
    # interface construction.
    sheets: list[tuple[Path, str, str, list[tuple[str, str, str]]]] = []
    for f in dit_files:
        try:
            text = read_text(f)
        except OSError:
            continue
        tag = _DIT_TYPE_TAG_RE.search(text)
        if not tag:
            continue
        rows = [
            (m.group("name"), m.group("scope"), m.group("type"))
            for m in _DIT_ROW_RE.finditer(text)
        ]
        if rows:
            sheets.append((f, tag.group("kind"), tag.group("name"), rows))

    if not sheets:
        return 0

    # --- learn ordinal -> type name from this project's own declarations ------
    learned: dict[int, Counter] = {}
    for _f, _kind, _name, rows in sheets:
        for var_name, _scope, type_token in rows:
            ord_match = _TYP_ORD_RE.fullmatch(type_token)
            if not ord_match:
                continue
            type_name = known.get(var_name)
            if type_name:
                learned.setdefault(int(ord_match.group(1)), Counter())[type_name] += 1

    def resolve(token: str) -> tuple[str, bool]:
        """Return ``(type_name, resolved)`` for a DIT type token."""

        m = _TYP_ORD_RE.fullmatch(token)
        if not m:
            return token, True
        ordinal = int(m.group(1))
        votes = learned.get(ordinal)
        if votes:
            top = votes.most_common(1)[0]
            # Only trust a unanimous winner; a tie between two different types
            # means the id is overloaded and we must not guess.
            if len(votes) == 1 or top[1] > votes.most_common(2)[1][1]:
                return top[0], True
        mapped = registered.get(ordinal)
        if mapped:
            return mapped, True
        builtin = _BUILTIN_TYPE_IDS.get(ordinal)
        if builtin and not votes:
            return builtin, True
        return f"@TYP:{ordinal}", False

    call_site_types = _observed_pin_types(project, known)

    declared = declared_pous(project)
    registered_fbs = 0
    unresolved_ids: set[str] = set()
    ambiguous_ids: set[int] = set()
    inferred_pins = 0

    for f, kind, name, rows in sheets:
        if kind != "FUNCTION_BLOCK":
            continue

        buckets: dict[str, list[Var]] = {}
        for var_name, scope, type_token in rows:
            type_name, resolved = resolve(type_token)
            pin = Var(name=var_name, type_name=type_name, scope=scope, source=SOURCE)
            if not resolved:
                # Last resort: if the project actually calls this block, the type
                # of whatever drives the pin tells us the pin's type. This is
                # inference from real code, not a guess, so it is flagged.
                observed = call_site_types.get((name, var_name))
                if observed:
                    pin.type_name = observed
                    pin.inferred = True
                    inferred_pins += 1
                    resolved = True
            if not resolved:
                unresolved_ids.add(type_name)
                ord_match = _TYP_ORD_RE.fullmatch(type_token)
                if ord_match and int(ord_match.group(1)) in learned:
                    ambiguous_ids.add(int(ord_match.group(1)))
            buckets.setdefault(scope, []).append(pin)

        # Declaration order: inOut, inputs, outputs, then locals.
        members: list[Var] = []
        for scope in _DIT_SCOPE_ORDER:
            members.extend(buckets.get(scope, []))

        is_library = name not in declared
        existing = project.types.get(name)
        if existing is not None and existing.members and not existing.is_library:
            # A project-declared type with real members wins.
            continue
        project.types[name] = TypeDef(
            name=name,
            kind="fb",
            members=members,
            source=SOURCE,
            is_library=is_library,
        )
        registered_fbs += 1

    if registered_fbs:
        project.add_note(
            f"recovered {registered_fbs} function-block interfaces from project "
            f".DIT metadata"
        )
    if inferred_pins:
        project.add_note(
            f"{inferred_pins} library pin types inferred from call sites in this "
            f"project (flagged typeInferred)"
        )
    if unresolved_ids:
        project.add_divergence(
            "unresolved_type_ids",
            "these type ids could not be resolved to a name (reported as-is rather "
            f"than guessed): {', '.join(sorted(unresolved_ids)[:12])}",
            [SOURCE],
        )
    if ambiguous_ids:
        project.add_divergence(
            "ambiguous_type_ids",
            "these type ids map to more than one type in this project, so pins "
            f"using them keep the raw id: {sorted(ambiguous_ids)[:12]}",
            [SOURCE],
        )
    return registered_fbs


def _observed_pin_types(project: Project, known: dict[str, str]) -> dict[tuple[str, str], str]:
    """``{(block type, pin name): driver type}`` from observed call sites.

    Used only to type library pins the ``.DIT`` left as a per-file local id. If
    the project calls ``MC_Power`` with ``Axis := TopCutter`` and ``TopCutter`` is
    declared ``AXIS_REF``, then that pin is ``AXIS_REF`` — a fact read out of real
    code rather than assumed.
    """

    out: dict[tuple[str, str], str] = {}
    for pou in project.pous.values():
        for net in pou.body.nets:
            for node in net.nodes:
                if node.kind != "block" or not node.type_name:
                    continue
                for pin, value in node.params.items():
                    if not value:
                        continue
                    head = value.split(".")[0]
                    type_name = known.get(head)
                    if type_name:
                        out.setdefault((node.type_name, pin), type_name)
    return out


def _registered_type_ids(root: Path) -> dict[int, str]:
    """``{type id: type name}`` from every ``TYLLIST.TYP`` under ``root``."""

    out: dict[int, str] = {}
    for f in sorted(root.rglob("TYLLIST.TYP")) + sorted(root.rglob("Tyllist.typ")):
        try:
            text = read_text(f)
        except OSError:
            continue
        for m in _TYL_ROW_RE.finditer(text):
            name = m.group("name")
            try:
                type_id = int(m.group("id"))
            except ValueError:
                continue
            if name:
                out.setdefault(type_id, name)
    return out


def _known_symbol_types(project: Project) -> dict[str, str]:
    """``{variable name: type name}`` from everything parsed so far.

    Ambiguous names (a name used with two different types) are dropped, because
    they would teach the ordinal table the wrong lesson.
    """

    votes: dict[str, Counter] = {}
    for var in project.global_vars:
        if var.name and var.type_name:
            votes.setdefault(var.name, Counter())[var.type_name] += 1
    for pou in project.pous.values():
        for var in pou.all_vars():
            if var.name and var.type_name:
                votes.setdefault(var.name, Counter())[var.type_name] += 1

    out: dict[str, str] = {}
    for name, counter in votes.items():
        if len(counter) == 1:
            out[name] = counter.most_common(1)[0][0]
    return out


# --------------------------------------------------------------------------
# NODES.LST — the project tree
# --------------------------------------------------------------------------


def _parse_nodes(root: Path, project: Project) -> None:
    """Read the tab-separated configuration tree.

    Rows look like ``PROGRAM\\t6\\tTopCutterCutControl\\tTopCutterCutControl\\teCLR\\tMP2600iec``
    — node kind, tree depth, name, call name, PLC type, processor type.
    """

    lst = root / "NODES.LST"
    if not lst.exists():
        # Nested exports place it under C/Configuration/.
        found = list(root.rglob("NODES.LST"))
        if not found:
            return
        lst = found[0]

    for line in read_text(lst).splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 3 or not parts[0]:
            continue
        entry = {
            "kind": parts[0],
            "depth": int(parts[1]) if parts[1].isdigit() else 0,
            "name": parts[2],
            "subName": parts[3] if len(parts) > 3 else "",
            "plcType": parts[4] if len(parts) > 4 else "",
            "procType": parts[5] if len(parts) > 5 else "",
        }
        project.tree.append(entry)
        if entry["kind"] in ("RESOURCE", "CONFIGURATION") and entry["procType"]:
            project.processor_type = project.processor_type or entry["procType"]
        if entry["kind"] == "RESOURCE" and entry["name"]:
            project.resource_type = project.resource_type or entry["name"]


# --------------------------------------------------------------------------
# eCLRPouDependencies.dat — the POU index
# --------------------------------------------------------------------------


def _parse_pou_dependencies(root: Path, project: Project) -> None:
    """Read the POU index: ids, kinds and dependencies.

    Format::

        PouDep.Cache;schema 1
        0,CalcBezier,3,FB
        4,CamGenerator,3,FB; 2,CalcSpline; 0,CalcBezier
        7,TopCutterCutControl,2,PG
        16,FastTsk,1,TA; 7,TopCutterCutControl

    The trailing ``; id,name`` pairs are dependencies, which for a task are its
    instantiated programs — that is how task membership is reconstructed here.
    """

    dat = None
    for candidate in root.rglob("eCLRPouDependencies.dat"):
        dat = candidate
        break
    if dat is None:
        return

    raw_tasks: dict[str, list[str]] = {}

    for line in read_text(dat).splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, deps = line.partition(";")
        fields = [f.strip() for f in head.split(",")]
        # Skip the schema banner ("PouDep.Cache,schema 1") and any malformed row.
        if len(fields) < 4 or not fields[0].isdigit():
            continue
        idx_s, name, _code, kind = fields[0], fields[1], fields[2], fields[3]
        if not name:
            continue
        pou_kind = _KIND.get(kind, kind.lower())
        entry = {
            "record": "poudep",
            "id": int(idx_s),
            "name": name,
            "kind": pou_kind,
        }
        project.tree.append(entry)

        if pou_kind == "task":
            raw_tasks[name] = [d.strip() for d in deps.split(";") if d.strip()]

    _materialise_tasks(project, raw_tasks)


def _materialise_tasks(project: Project, raw: dict[str, list[str]]) -> None:
    """Turn the dependency-derived task membership into :class:`Task` objects."""

    if not raw:
        return

    # Map id -> POU name from the index we just built.
    id_to_name: dict[int, str] = {
        entry["id"]: entry["name"]
        for entry in project.tree
        if entry.get("record") == "poudep" and isinstance(entry.get("id"), int)
    }

    for task_name, deps in raw.items():
        programs: list[str] = []
        for dep in deps:
            for token in dep.split(","):
                token = token.strip()
                if token.isdigit():
                    nm = id_to_name.get(int(token))
                    if nm and nm not in programs:
                        programs.append(nm)
        task = project.tasks.get(task_name)
        if task is None:
            task = Task(name=task_name, source=SOURCE)
            project.tasks[task_name] = task
        for p in programs:
            if p not in task.programs:
                task.programs.append(p)


# --------------------------------------------------------------------------
# OCIRES.INI — processor identity
# --------------------------------------------------------------------------


def _parse_resource_ini(root: Path, project: Project) -> None:
    for ini in root.rglob("OCIRES.INI"):
        text = read_text(ini)
        m = _PROCTYPE_RE.search(text)
        if m:
            project.processor_type = m.group("v")
        rt = re.search(r"ResourceProcType\s*=\s*([A-Za-z0-9_]+)", text)
        if rt and not project.resource_type:
            project.resource_type = rt.group(1)
        return


# --------------------------------------------------------------------------
# *.SET — task configuration
# --------------------------------------------------------------------------


def _parse_task_sets(root: Path, project: Project) -> None:
    """Read ``<Task>.SET`` files, which hold the IEC task configuration.

    Parameters are collected by scanning the whole file for ``KEY := value``
    pairs rather than by first isolating the ``TASK ... ( ... )`` body. The body
    contains no closing ``)`` of its own, so a body capture runs to the end of
    the file and drags later keys into the previous key's value.
    """

    for setfile in root.rglob("*.SET"):
        try:
            text = read_text(setfile)
        except OSError:
            continue
        m = _TASK_SET_RE.search(text)
        if not m:
            continue
        name = m.group("name")
        params = {
            kv.group("key").upper(): kv.group("val").strip()
            for kv in _KV_RE.finditer(text)
        }
        task = project.tasks.get(name)
        if task is None:
            task = Task(name=name, source=SOURCE)
            project.tasks[name] = task
        task.task_type = task.task_type or params.get("TYPE", "").strip()
        task.interval = task.interval or params.get("INTERVAL", "").strip()
        priority = params.get("PRIORITY", "").strip()
        if task.priority is None and priority.isdigit():
            task.priority = int(priority)
        task.watchdog = task.watchdog or params.get("WATCHDOG", "").strip()


def _parse_project_info(root: Path, project: Project) -> None:
    inf = root / "PROJECT.INF"
    if inf.exists():
        for line in read_text(inf).splitlines():
            if line.startswith("LastChange"):
                project.creation = line.partition("=")[2].strip()


# --------------------------------------------------------------------------
# Helpers used by the merge step
# --------------------------------------------------------------------------


def declared_pous(project: Project) -> dict[str, dict]:
    """Return ``{name: {kind, id}}`` for POUs the native POU index declares."""

    out: dict[str, dict] = {}
    for entry in project.tree:
        if entry.get("record") != "poudep":
            continue
        if entry.get("kind") in ("program", "function_block", "function"):
            out[entry["name"]] = {"kind": entry["kind"], "id": entry.get("id")}
    return out


def language_hint(path: Path) -> str:
    """Best-effort language guess for a POE folder (it never carries code)."""

    for f in path.glob("*.cfb"):
        return Language.FBD.value
    return Language.UNKNOWN.value
