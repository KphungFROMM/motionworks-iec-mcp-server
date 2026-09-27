"""Parse an Extended IEC 61131-2 export directory.

This export is the *readable* projection of a MotionWorks IEC project: plain
text, one file per POU, with ``(*@PROPERTIES_EX@ ... *)`` headers and
``(*@KEY@: NAME ... *)`` region markers. It is the only source that gives us
straight-line Structured Text without HTML wrapping.

A directory looks like::

    <Project>/
        <Pou>.ST / <Pou>.GE / <Pou>.IEC / <Pou>.DIT
        PHYSHARDWARE.EXP
        Configuration/
            CONFIGURATION.EXP
            Resource/
                Global_Variables.GVB
                IOCONFIGURATION.EIO
                FastTsk.EXP  MedTsk.EXP  RESOURCE.EXP  ...

Note the resource folder is sometimes ``Resource/`` and sometimes
``R/Resource/`` depending on the export, so we locate files by name rather than
by a fixed relative path.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..model import (
    Body,
    IoPoint,
    Language,
    Pou,
    Project,
    Scope,
    SourceKind,
    Task,
    TypeDef,
    Var,
)
from ..util import (
    extract_description,
    extract_key_block,
    parse_directives,
    read_text,
    strip_html,
)
from . import st as st_parser

SOURCE = SourceKind.EXTENDED_IEC.value

POU_EXTENSIONS = {".st", ".ge", ".iec", ".dit"}
_TYPE_EXTENSIONS = {".typ", ".iec"}

# POU name -> language, inferred from the file extension when the header is
# absent (the header is authoritative when present).
EXT_LANGUAGE = {
    ".st": Language.ST,
    ".ge": Language.UNKNOWN,   # LD or FBD — the header tells us which
    ".iec": Language.UNKNOWN,
    ".dit": Language.UNKNOWN,
}

_PROCTYPE_RE = re.compile(r"PROCTYPE\s*:\s*'?([A-Za-z0-9_]+)'?", re.IGNORECASE)
_PLCTYPE_RE = re.compile(r"PLCTYPE\s*:\s*'?([A-Za-z0-9_]+)'?", re.IGNORECASE)
_NAME_RE = re.compile(r"NAME\s*:\s*'?([^'\n)]+)'?", re.IGNORECASE)
_TYPE_TAG_RE = re.compile(r"T:\s*([A-Z_]+)\s+([A-Za-z_][A-Za-z0-9_]*)")

_COMMENT_RE = re.compile(r"\(\*.*?\*\)", re.DOTALL)


def is_extended_export(path: str | Path) -> bool:
    """Heuristic: does this directory look like an Extended IEC export?

    Deliberately broad, because a folder may legitimately hold only types or only
    task configuration. The parsers themselves are tolerant: an unrecognised file
    is skipped, and a folder with nothing usable simply contributes nothing.
    """

    p = Path(path)
    if not p.is_dir():
        return False
    if any(p.glob("*.GVB")) or any(p.rglob("Global_Variables.GVB")):
        return True
    if any(p.glob("PHYSHARDWARE.EXP")):
        return True
    # A folder of POU / type files carrying the @PROPERTIES_EX@ header.
    for pattern in ("*.ST", "*.GE", "*.IEC", "*.DIT"):
        for f in list(p.glob(pattern))[:4]:
            try:
                if "@PROPERTIES_EX@" in read_text(f)[:400]:
                    return True
            except OSError:
                continue
    return False


def parse(root: str | Path, project: Project) -> Project:
    """Populate ``project`` from an Extended IEC export directory."""

    root = Path(root)
    # Guard against being handed the same export twice (a caller merging several
    # source folders can easily pass a nested one). Without this, every variable,
    # I/O point and task would be appended a second time.
    if SOURCE in project.sources and project.source_paths.get(SOURCE) == str(root):
        return project
    project.sources.append(SOURCE)
    project.source_paths[SOURCE] = str(root)
    if not project.name:
        project.name = root.name

    _register_source_files(root, project)
    _parse_pous(root, project)
    _parse_dit_types(root, project)
    _parse_global_vars(root, project)
    _parse_tasks(root, project)
    _parse_io(root, project)
    _parse_types(root, project)
    _parse_translations(root, project)
    return project


def _register_source_files(root: Path, project: Project) -> None:
    """Record generator/version metadata if it is present."""

    for name in ("@@@$.ini",):
        f = root / name
        if f.exists():
            project.notes.append(f"generator info present: {name}")


# --------------------------------------------------------------------------
# POUs
# --------------------------------------------------------------------------


def _parse_pous(root: Path, project: Project) -> None:
    for f in sorted(root.iterdir()):
        if not f.is_file() or f.suffix.lower() not in POU_EXTENSIONS:
            continue
        try:
            _parse_pou_file(f, project, root)
        except Exception as exc:  # never let one bad file sink the project
            project.notes.append(f"failed to parse {f.name}: {exc}")


def _parse_pou_file(f: Path, project: Project, root: Path) -> None:
    text = read_text(f)
    props = parse_directives(text)

    kind = props.get("TYPE", "").upper()
    if kind == "DATA_TYPE":
        _parse_type_file(f, text, project)
        return

    language = props.get("IEC_LANGUAGE", "").upper()
    if not language:
        language = "ST" if f.suffix.lower() == ".st" else ""

    header, decl_text, body_text = st_parser.split_pou_file(text)
    pou_type, name, _ret = st_parser.parse_pou_header(text)
    if not name:
        name = f.stem
    if not pou_type:
        pou_type = "program"

    if kind == "DATA_TYPE" or not name:
        return

    pou = Pou(
        name=name,
        pou_type=pou_type,
        language=(language or Language.UNKNOWN.value),
        description=extract_description(text),
        file_path=str(f),
        source=SOURCE,
        project_name=root.name,
    )

    # Declarations: prefer the real declaration text, fall back to a .DIT
    # sibling when the POU body carries its interface elsewhere.
    pou.vars = st_parser.parse_declarations(decl_text or header, source=SOURCE)
    dit = f.with_suffix(".DIT")
    if dit.exists() and not pou.vars:
        pou.vars = st_parser.parse_dit_declarations(read_text(dit), source=SOURCE)

    if pou.language == Language.ST.value:
        code = body_text.strip()
        pou.body = Body(
            language=pou.language,
            code=code,
            statements=st_parser.count_statements(code),
        )
    elif pou.language in (Language.LD.value, Language.FBD.value):
        # The graphical body is decoded by parsers.graphical, called from
        # project.py so the .GE decoder stays independently testable.
        pou.body = Body(language=pou.language, code=body_text.strip())
    else:
        pou.body = Body(
            language=pou.language or Language.UNKNOWN.value,
            unsupported=f"language {pou.language or '?'} is not rendered by this server",
        )

    project.pous[pou.name] = pou
    project.pou_versions.append(pou)
    project.record_origin(SOURCE, pou.project_name, pou.name)


# --------------------------------------------------------------------------
# Global variables
# --------------------------------------------------------------------------


def _parse_global_vars(root: Path, project: Project) -> None:
    gvb: Path | None = None
    candidates = list(root.rglob("Global_Variables.GVB"))
    if candidates:
        gvb = candidates[0]
    if gvb is None:
        return
    text = read_text(gvb)
    scope_map = st_parser.parse_declarations(text, source=SOURCE)
    for scope, vs in scope_map.items():
        for v in vs:
            v.scope = Scope.VAR_GLOBAL.value
            project.global_vars.append(v)

    # Group headers appear as `(*Group:NAME*)` immediately before the block.
    _apply_group_comments(text, project.global_vars)


def _apply_group_comments(text: str, vars_list: list[Var]) -> None:
    """Attach ``(*Group:...*)`` headings to the following globals.

    Appended rather than assigned: a variable usually also carries an inline
    comment, and the group heading is the only place the amplifier/node context
    (``AXIS3 <SGD7S> ...``) is recorded.

    The heading is held until the first declaration inside the ``VAR...END_VAR``
    block that follows it. Clearing it on the ``VAR_GLOBAL`` line itself was a
    bug — that line is neither a heading nor a declaration, so the heading was
    discarded before any variable could claim it.
    """

    pending = ""
    in_block = False

    for line in text.splitlines():
        gm = re.match(r"\s*\(\*Group\s*:\s*(?P<g>.*?)\*\)", line)
        if gm:
            pending = gm.group("g").strip()
            continue
        if re.match(r"\s*END_VAR\b", line, re.IGNORECASE):
            in_block = False
            continue
        if re.match(r"\s*VAR[A-Z_]*\b", line, re.IGNORECASE):
            in_block = True
            continue
        if not in_block:
            continue
        dm = re.match(r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b", line)
        if not dm:
            continue
        for v in vars_list:
            if v.name == dm.group("name"):
                if pending and f"[{pending}]" not in v.comment:
                    v.comment = f"[{pending}] {v.comment}".strip()
                break


# --------------------------------------------------------------------------
# Tasks and resource
# --------------------------------------------------------------------------


def _parse_tasks(root: Path, project: Project) -> None:
    for f in root.rglob("*.EXP"):
        try:
            text = read_text(f)
        except OSError:
            continue
        stem = f.stem.upper()
        if stem == "RESOURCE":
            _parse_resource(text, project)
        elif stem == "CONFIGURATION":
            m = _NAME_RE.search(text)
            if m:
                project.notes.append(f"configuration: {m.group(1).strip()}")
        elif stem == "PHYSHARDWARE":
            continue
        else:
            task = _parse_task(text, f)
            if task:
                project.tasks[task.name] = task


def _parse_task(text: str, f: Path) -> Task | None:
    m = re.search(r"\(\*@KEY@\s*:?\s*TASK(?P<body>.*?)\*\)", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    head = m.group("body")
    name_m = _NAME_RE.search(head)
    if not name_m:
        return None
    name = name_m.group(1).strip()

    ttype_m = re.search(r"TASKTYPE\s*:\s*([A-Za-z_]+)", head, re.IGNORECASE)
    priority_m = re.search(r"PRIORITY\s*:=\s*(\d+)", text, re.IGNORECASE)
    interval_m = re.search(r"INTERVAL\s*:=\s*(T#[0-9A-Za-z_.]+)", text, re.IGNORECASE)
    watchdog_m = re.search(r"WATCHDOG\s*:=\s*(\d+)", text, re.IGNORECASE)
    tasktype_m = re.search(r"TYPE\s*:=\s*([A-Za-z_]+)", text, re.IGNORECASE)

    programs: list[str] = []
    for pg in re.finditer(
        r"\(\*@KEY@\s*:?\s*PGINSTANCE(?P<b>.*?)\*\)", text, re.DOTALL | re.IGNORECASE
    ):
        nm = _NAME_RE.search(pg.group("b"))
        if nm:
            programs.append(nm.group(1).strip())

    return Task(
        name=name,
        task_type=(ttype_m.group(1) if ttype_m else (tasktype_m.group(1) if tasktype_m else "")),
        interval=interval_m.group(1) if interval_m else "",
        priority=int(priority_m.group(1)) if priority_m else None,
        watchdog=watchdog_m.group(1) if watchdog_m else "",
        programs=programs,
        source=SOURCE,
    )


def _parse_resource(text: str, project: Project) -> None:
    m = re.search(
        r"\(\*@KEY@\s*:?\s*RESOURCE(?P<body>.*?)\*\)", text, re.DOTALL | re.IGNORECASE
    )
    head = m.group("body") if m else text
    nm = _NAME_RE.search(head)
    if nm:
        project.resource_type = project.resource_type or nm.group(1).strip()
    pt = _PROCTYPE_RE.search(text)
    if pt:
        project.processor_type = project.processor_type or pt.group(1)
    plt = _PLCTYPE_RE.search(text)
    if plt:
        project.product_version = project.product_version or plt.group(1)


# --------------------------------------------------------------------------
# I/O configuration
# --------------------------------------------------------------------------


def _parse_io(root: Path, project: Project) -> None:
    found = list(root.rglob("IOCONFIGURATION.EIO"))
    if not found:
        return
    text = read_text(found[0])
    # Split into `PROGRAM <name> WITH <task> : <DIRECTION> ( ... );` entries.
    pattern = re.compile(
        r"(?:\(\*(?P<group>.*?)\*\))?\s*"
        r"PROGRAM\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+WITH\s+(?P<task>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*:\s*(?P<dir>INPUT|OUTPUT)\s*\((?P<params>.*?)\)\s*;",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        params = _parse_kv_params(m.group("params"))
        project.io_points.append(
            IoPoint(
                name=m.group("name"),
                direction=m.group("dir").upper(),
                group=(m.group("group") or "").strip(),
                task=m.group("task"),
                var_addr=params.get("VAR_ADR", ""),
                end_var_addr=params.get("END_VAR_ADR", ""),
                device=params.get("DEVICE", ""),
                driver_name=params.get("DRIVER_NAME", "").strip("'"),
                driver_params=[
                    params.get(f"DRIVER_PAR{i}", "") for i in range(1, 5)
                ],
                data_type=params.get("DATA_TYPE", ""),
                source=SOURCE,
            )
        )


def _parse_kv_params(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(
        r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:=\s*(?P<val>'[^']*'|[^,\n)]+)", text
    ):
        out[m.group("key").upper()] = m.group("val").strip()
    return out


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------


def _parse_types(root: Path, project: Project) -> None:
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() not in _TYPE_EXTENSIONS:
            continue
        try:
            text = read_text(f)
        except OSError:
            continue
        if "END_TYPE" not in text.upper():
            continue
        _parse_type_file(f, text, project)


def _parse_type_file(f: Path, text: str, project: Project) -> None:
    """Parse ``TYPE ... END_TYPE`` declarations from a .IEC / .TYP file.

    Four details are load-bearing:

    * comments are removed *before* locating ``TYPE``, because the file's own
      ``(*@PROPERTIES_EX@ ... TYPE: DATA_TYPE ... *)`` header contains that word
      and would otherwise be mistaken for the start of the block;
    * a comment is replaced by ``;``, not whitespace: a trailing comment *is* a
      statement terminator, so blanking it to a space welds the declaration
      before it to the one after it;
    * struct bodies are located by their own ``STRUCT ... END_STRUCT`` boundaries
      and only then split on ``;``. Splitting the whole type region on ``;``
      first truncates the struct at its first internal semicolon, so only the
      first member of every struct survives;
    * a non-struct declaration is taken up to the first top-level ``;``.
    """

    cleaned = _COMMENT_RE.sub(";", text)

    start = re.search(r"\bTYPE\b", cleaned, re.IGNORECASE)
    if not start:
        return
    body = cleaned[start.end() :]
    end = re.search(r"\bEND_TYPE\b", body, re.IGNORECASE)
    if end:
        body = body[: end.start()]

    comments = _comment_map(text)
    consumed: list[tuple[int, int]] = []

    # --- structs, by their own boundaries ---------------------------------
    struct_re = re.compile(
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*STRUCT\b"
        r"(?P<members>.*?)\bEND_STRUCT\b",
        re.IGNORECASE | re.DOTALL,
    )
    for m in struct_re.finditer(body):
        name = m.group("name")
        td = TypeDef(name=name, kind="struct", source=SOURCE)
        for member in st_parser.split_statements(m.group("members")):
            member = member.strip()
            if not member:
                continue
            mm = re.match(
                r"^(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<t>.*)$", member, re.DOTALL
            )
            if not mm:
                continue
            type_text = mm.group("t").strip()
            initial = ""
            if ":=" in type_text:
                type_text, _, initial = type_text.partition(":=")
                initial = initial.strip()
            var = Var(
                name=mm.group("n"),
                type_name=type_text.strip(),
                initial=initial,
                source=SOURCE,
            )
            var.comment = comments.get(var.name, "")
            td.members.append(var)
        project.types.setdefault(name, td)
        consumed.append(m.span())

    # --- everything else: enum, array, alias ------------------------------
    remainder = list(body)
    for a, b in consumed:
        for i in range(a, b):
            remainder[i] = " "
    rest_text = "".join(remainder)

    for stmt in st_parser.split_statements(rest_text):
        stmt = stmt.strip()
        if not stmt:
            continue
        head = re.match(
            r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<rest>.*)$", stmt, re.DOTALL
        )
        if not head:
            continue
        name = head.group("name")
        rest = head.group("rest").strip()
        if not name or name.upper() in {"TYPE", "END_TYPE"} or "STRUCT" in rest.upper():
            continue

        upper = rest.upper()
        if "(" in rest and ")" in rest:
            td = TypeDef(name=name, kind="enum", source=SOURCE)
            inner = rest[rest.index("(") + 1 : rest.rindex(")")]
            for i, item in enumerate(inner.split(",")):
                item = item.strip()
                if not item:
                    continue
                if ":=" in item:
                    k, _, v = item.partition(":=")
                    td.enum_values.append({"name": k.strip(), "value": v.strip()})
                else:
                    td.enum_values.append({"name": item, "value": str(i)})
            project.types.setdefault(name, td)
        elif "ARRAY" in upper:
            arr = re.search(
                r"ARRAY\s*\[(?P<dims>[^\]]*)\]\s*OF\s*(?P<base>.+)",
                rest,
                re.IGNORECASE | re.DOTALL,
            )
            if arr:
                project.types.setdefault(
                    name,
                    TypeDef(
                        name=name,
                        kind="array",
                        base_type=arr.group("base").strip(),
                        comment=arr.group("dims").strip(),
                        source=SOURCE,
                    ),
                )
        else:
            project.types.setdefault(
                name, TypeDef(name=name, kind="alias", base_type=rest, source=SOURCE)
            )


def _comment_map(text: str) -> dict[str, str]:
    """Map declaration name -> the ``(* comment *)`` that followed it.

    Scanned manually rather than with a regex: comments routinely contain ``*``
    (``(*  Part length in mm  *)``), which makes any ``\\(\\*.*?\\*\\)`` pattern
    match at the wrong offsets.

    The declaration name is found by walking *backwards* from the comment over
    the declaration, because the declaration's own ``;`` is the last character
    before a trailing comment — so any forward-looking pattern that stops at a
    semicolon (or refuses to cross one) finds nothing at all.
    """

    out: dict[str, str] = {}
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "(" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*)", i + 2)
            if end == -1:
                break
            body = text[i + 2 : end].strip()
            if not body or set(body) <= {"*", " "}:
                i = end + 2
                continue
            # Walk back over the optional `;` that terminates the declaration and
            # any whitespace, then read the *type*, then the `:`, then the name.
            # Reading the last identifier directly would yield the type name.
            j = i - 1
            while j >= 0 and text[j] in "\t\r\n ;":
                j -= 1
            k = j
            while k >= 0 and (text[k].isalnum() or text[k] == "_"):
                k -= 1
            j = k
            while j >= 0 and text[j] in "\t\r\n ":
                j -= 1
            if j < 0 or text[j] != ":":
                i = end + 2
                continue
            j -= 1
            while j >= 0 and text[j] in "\t\r\n ":
                j -= 1
            k = j
            while k >= 0 and (text[k].isalnum() or text[k] == "_"):
                k -= 1
            name = text[k + 1 : j + 1]
            if name and not name[0].isdigit():
                out.setdefault(name, body)
            i = end + 2
            continue
        i += 1
    return out


def _parse_dit_types(root: Path, project: Project) -> None:
    """Read ``.DIT`` files to recover function-block interfaces.

    ``T: FUNCTION_BLOCK <Name>`` in the header tells us it is an FB; the rows
    below it are its formal parameters in declaration order.
    """

    for f in sorted(root.rglob("*.DIT")):
        try:
            text = read_text(f)
        except OSError:
            continue
        tm = _TYPE_TAG_RE.search(text)
        if not tm:
            continue
        kind_tag, name = tm.group(1), tm.group(2)
        members = st_parser.parse_dit_declarations(text, source=SOURCE)
        flat: list[Var] = []
        for scope in (
            Scope.VAR_INPUT.value,
            Scope.VAR_IN_OUT.value,
            Scope.VAR_OUTPUT.value,
            Scope.VAR.value,
        ):
            flat.extend(members.get(scope, []))
        kind = "fb" if kind_tag == "FUNCTION_BLOCK" else kind_tag.lower()
        project.types.setdefault(
            name, TypeDef(name=name, kind=kind, members=flat, source=SOURCE, is_library=True)
        )


# --------------------------------------------------------------------------
# Translations
# --------------------------------------------------------------------------


def _parse_translations(root: Path, project: Project) -> None:
    """Index localization tables so comments can be resolved by id."""

    import xml.etree.ElementTree as ET

    for f in root.rglob("*Translation.xml"):
        try:
            text = read_text(f)
            root_el = ET.fromstring(text)
        except (OSError, ET.ParseError):
            continue
        for item in root_el.iter("item"):
            item_id = item.get("id", "")
            tr = item.find("translation")
            if tr is not None and tr.text and item_id:
                key = f"{f.stem}#{item_id}"
                project.translations.setdefault(key, tr.text)
