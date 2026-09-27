"""IEC 61131-3 Structured Text parsing: declaration blocks and POU headers.

Every MotionWorks POU — regardless of whether its *body* language is ST, LD or
FBD — carries its interface as normal IEC declaration blocks (``VAR``,
``VAR_EXTERNAL``, ...). This module turns those blocks into :class:`Var`
objects and splits a POU file into ``(header, declarations, body)``.

The declarations come in two flavours and both must work:

* Real source (``.ST``, ``.IEC``)::

      VAR_EXTERNAL
          Master : AXIS_REF;(*External Encoder - 21*)
      END_VAR

* POU metadata (``.DIT``) whose fields are tab-separated with a
  ``@TYP:<ordinal>`` type reference instead of a type name::

      @V 1 6 0
      CamData	1	VAR_IN_OUT	@TYP:1306

For the second flavour we keep the name, scope and declared *slot* so callers
can still see the interface, and mark the type as ``@TYP:<n>`` rather than
inventing a name.
"""

from __future__ import annotations

import re

from ..model import Scope, Var
from ..util import QUALIFIED_IDENT_RE

# Declaration block keywords, longest first so VAR_INPUT wins over VAR.
BLOCK_KEYWORDS = (
    "VAR_IN_OUT",
    "VAR_EXTERNAL",
    "VAR_INPUT",
    "VAR_OUTPUT",
    "VAR_GLOBAL",
    "VAR_TEMP",
    "VAR_RETAIN",
    "VAR_CONFIG",
    "VAR_ACCESS",
    "VAR",
)

_END_VAR_RE = re.compile(r"\bEND_VAR\b", re.IGNORECASE)

# `name AT %addr : type := init ;`
# `name AT %addr : type := init ;`
# NOTE: callers split the block on ';' before matching, so the trailing
# semicolon is usually already gone — it must be optional. Requiring it silently
# drops every declaration in the file, which is the bug that produced empty
# global-variable tables until it was caught.
_DECL_RE = re.compile(
    r"""
    ^\s*
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    \s*
    (?P<rest>.*?)
    \s*;?\s*$
    """,
    re.VERBOSE | re.DOTALL,
)

_AT_RE = re.compile(r"\bAT\s+(?P<addr>%[A-Za-z0-9_.]+)", re.IGNORECASE)
_INIT_RE = re.compile(r":=\s*(?P<init>.*)$", re.DOTALL)
_ARRAY_RE = re.compile(r"\bARRAY\s*\[(?P<dims>[^\]]*)\]\s*OF\s*(?P<base>.+)", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"\(\*.*?\*\)", re.DOTALL)

# POU header: PROGRAM Name / FUNCTION_BLOCK Name / FUNCTION Name : RetType
_POU_HEADER_RE = re.compile(
    r"^\s*(?P<kind>PROGRAM|FUNCTION_BLOCK|FUNCTION)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*(?::\s*(?P<ret>[A-Za-z_][A-Za-z0-9_]*))?",
    re.IGNORECASE | re.MULTILINE,
)

# End keywords that may close a POU body.
_END_POU_RE = re.compile(
    r"^\s*END_(?:PROGRAM|FUNCTION_BLOCK|FUNCTION)\b", re.IGNORECASE | re.MULTILINE
)

# `(*@KEY@: WORKSHEET ... *)` ... `(*@KEY@: END_WORKSHEET *)`
_WORKSHEET_OPEN_RE = re.compile(r"\(\*@KEY@\s*:?\s*WORKSHEET.*?\*\)", re.DOTALL | re.IGNORECASE)
_WORKSHEET_CLOSE_RE = re.compile(
    r"\(\*@KEY@\s*:?\s*END_WORKSHEET\s*\*\)", re.IGNORECASE
)

_DIT_TYPE_RE = re.compile(r"@TYP:(\d+)")
_DIT_ROW_RE = re.compile(r"^\s*(?P<slot>\d+)\s+(?P<scope>VAR_[A-Z_]+)\s+(?P<type>@TYP:\d+)\s*$")


def strip_comments(text: str) -> str:
    """Remove ``(* ... *)`` comments (non-nesting is fine here — see note)."""

    return _COMMENT_RE.sub(" ", text)


def _split_type_and_init(rest: str) -> tuple[str, str, str, str]:
    """Split a declaration tail into (address, type, initial, dimensions)."""

    address = ""
    at = _AT_RE.search(rest)
    if at:
        address = at.group("addr")
        rest = rest[: at.start()] + " " + rest[at.end() :]

    initial = ""
    init = _INIT_RE.search(rest)
    if init:
        initial = init.group("init").strip()
        rest = rest[: init.start()]

    type_text = " ".join(rest.split())
    # A leading ':' separates the type from any address/attribute clause
    # (`AT %MD1.0 :\tDINT`). Left in place it becomes part of the type name.
    type_text = type_text.lstrip(":").strip()
    dimensions = ""
    arr = _ARRAY_RE.match(type_text)
    if arr:
        dimensions = arr.group("dims").strip()
        type_text = arr.group("base").strip()

    return address, type_text, initial, dimensions


def parse_declarations(text: str, source: str = "") -> dict[str, list[Var]]:
    """Parse every declaration block in ``text`` into ``scope -> [Var]``.

    Statement text containing the literal word ``VAR`` is not at risk because we
    only look for a keyword that starts a line (allowing leading indentation).
    """

    found: dict[str, list[Var]] = {}
    # Match a block keyword at the start of a line, then its END_VAR.
    block_re = re.compile(
        r"^[ \t]*(?P<kw>" + "|".join(BLOCK_KEYWORDS) + r")\b(?P<body>.*?)\bEND_VAR\b",
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )

    for m in block_re.finditer(text):
        scope = m.group("kw").upper()
        body = m.group("body")

        # `.DIT` style rows are handled by parse_dit_declarations(); a `.DIT`
        # embedded in a .ST file never happens, so nothing to do here.
        if body.strip():
            _parse_var_body(body, scope, found, source)

    return found


def split_statements(text: str) -> list[str]:
    """Split IEC source into statements on top-level ``;``.

    A plain ``text.split(";")`` is wrong, because a ``;`` inside a comment is
    common in real code (``(* pos := 1; see note *)``) and would fabricate a
    statement. This walks the text tracking comment state instead.
    """

    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "(" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*)", i + 2)
            if end == -1:
                buf.append(text[i:])          # unterminated comment: keep it all
                break
            buf.append(text[i : end + 2])
            i = end + 2
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def _parse_var_body(body: str, scope: str, found: dict[str, list[Var]], source: str) -> None:
    """Parse the statements of one declaration block."""

    # Guard: if this looks like a .DIT block, the row parser handled it.
    if _DIT_ROW_RE.search(body):
        return

    # Blank out comments but keep their positions so the per-line comment
    # attachment below can still find them.
    cleaned = _COMMENT_RE.sub(lambda m: " " * len(m.group(0)), body)

    for stmt in split_statements(cleaned):
        stmt = stmt.strip()
        if not stmt:
            continue
        dm = _DECL_RE.match(stmt)
        if not dm:
            continue
        name = dm.group("name")
        if name.upper() in {k.upper() for k in BLOCK_KEYWORDS} or name.upper() == "END_VAR":
            continue
        address, type_name, initial, dimensions = _split_type_and_init(dm.group("rest"))
        retain = scope == Scope.VAR_RETAIN.value
        found.setdefault(scope, []).append(
            Var(
                name=name,
                type_name=type_name,
                scope=scope,
                address=address,
                initial=initial,
                dimensions=dimensions,
                retain=retain,
                source=source,
            )
        )

    # Attach `(*comment*)` text to the declaration it followed on the same line.
    _attach_comments(body, found, scope)


def _attach_comments(body: str, found: dict[str, list[Var]], scope: str) -> None:
    """Best-effort: pair each trailing ``(*...*)`` with the declaration on its line."""

    vars_in_scope = found.get(scope, [])
    if not vars_in_scope:
        return
    by_name = {v.name: v for v in vars_in_scope if v.name}
    for line in body.splitlines():
        cm = _COMMENT_RE.search(line)
        if not cm:
            continue
        text = line[: cm.start()]
        dm = re.match(r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)", text)
        if not dm:
            continue
        target = by_name.get(dm.group("name"))
        if target is None or target.comment:
            continue
        comment = cm.group(0)[2:-2].strip()
        # A declaration-only marker such as (**) carries no information.
        if comment and not set(comment) <= {"*", " "}:
            target.comment = comment


def parse_dit_declarations(text: str, source: str = "") -> dict[str, list[Var]]:
    """Parse a ``.DIT`` metadata file into ``scope -> [Var]``.

    Rows look like ``CamData\\t1\\tVAR_IN_OUT\\t@TYP:1306``. The first token is
    the variable name, the batch index follows, then the scope, then the type
    reference. These files give us the *interface* of library blocks whose
    implementation is supplied by the firmware DLLs.
    """

    found: dict[str, list[Var]] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if "@V " in line:
            continue
        if not line.startswith("\t") and "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if len(parts) >= 3 and parts[2].upper().startswith("VAR_"):
                name, slot, scope, type_ref = parts[0], parts[1], parts[2], parts[3]
                found.setdefault(scope.upper(), []).append(
                    Var(
                        name=name,
                        type_name=type_ref,
                        scope=scope.upper(),
                        source=source,
                    )
                )
    return found


def parse_pou_header(text: str) -> tuple[str, str, str]:
    """Return ``(pou_type, name, return_type)`` from a POU header line."""

    m = _POU_HEADER_RE.search(text)
    if not m:
        return "", "", ""
    kind = m.group("kind").upper()
    mapping = {
        "PROGRAM": "program",
        "FUNCTION_BLOCK": "function_block",
        "FUNCTION": "function",
    }
    return mapping.get(kind, kind.lower()), m.group("name"), (m.group("ret") or "")


def split_pou_file(text: str) -> tuple[str, str, str]:
    """Split an Extended-IEC POU file into ``(header_block, declarations, body)``.

    ``header_block`` is everything up to and including the ``PROGRAM``/
    ``FUNCTION_BLOCK`` line; ``declarations`` is the concatenation of all
    declaration blocks; ``body`` is the implementation (ST statements, or the
    ``(*@KEY@: WORKSHEET ... *)`` region for LD/FBD).
    """

    header_m = _POU_HEADER_RE.search(text)
    header = text[: header_m.end()] if header_m else text
    after_header = text[header_m.end() :] if header_m else ""

    # Prefer the explicit worksheet region when the file has one.
    ws = _WORKSHEET_OPEN_RE.search(after_header)
    if ws:
        decl = after_header[: ws.start()]
        close = _WORKSHEET_CLOSE_RE.search(after_header, ws.end())
        body = after_header[ws.end() : close.start()] if close else after_header[ws.end() :]
        return header, decl, body

    # Otherwise: everything from the first declaration block to END_* is body.
    end_m = _END_POU_RE.search(after_header)
    body_end = end_m.start() if end_m else len(after_header)

    blocks = list(
        re.finditer(
            r"^[ \t]*(?:" + "|".join(BLOCK_KEYWORDS) + r")\b.*?\bEND_VAR\b",
            after_header,
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
    )
    if blocks:
        decl = after_header[: blocks[-1].end()]
        body = after_header[blocks[-1].end() : body_end]
    else:
        decl = ""
        body = after_header[:body_end]
    return header, decl, body


def count_statements(code: str) -> int:
    """Count executable statements in an ST body.

    Uses the same comment-aware splitter as the declaration parser, so a
    semicolon inside a comment cannot inflate the count. A fragment that turns
    out to be nothing but comments is not a statement — without that check a
    bare ``(* note *)`` would be counted as one.
    """

    if not code:
        return 0
    count = 0
    for stmt in split_statements(code):
        if _COMMENT_RE.sub("", stmt).strip():
            count += 1
    return count


def extract_identifiers(code: str, keywords: frozenset[str]) -> set[str]:
    """Collect lexical identifiers from ST code, excluding IEC keywords.

    Returns both bare and qualified forms, so ``TopCutter_PSET`` and
    ``Products.Sensor.Bit`` are both discoverable as symbols.
    """

    out: set[str] = set()
    if not code:
        return out
    cleaned = _COMMENT_RE.sub(" ", code)
    for m in QUALIFIED_IDENT_RE.finditer(cleaned):
        token = m.group(1)
        if token.upper() in keywords:
            continue
        out.add(token)
    return out
