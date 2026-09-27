"""Shared low-level helpers: tolerant file reading and IEC text handling."""

from __future__ import annotations

import html
import re
from pathlib import Path

# Yaskawa writes UTF-8 with BOM for some files and cp1252 for others (the
# translation XMLs are UTF-16). Try in order, never raise on a decode mismatch.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "utf-16")

_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# (*@PROPERTIES_EX@  ...  *)  and  (*@KEY@:NAME ... *)
_PROPERTIES_RE = re.compile(
    r"\(\*@PROPERTIES_EX@(?P<body>.*?)\*\)", re.DOTALL | re.IGNORECASE
)
# The writer emits `(*@KEY@:NAME` with no space after the colon, and
# `(*@KEY@: NAME` with one. The name is letters/digits/underscore only — allowing
# a space here makes the name greedy and it swallows the body, which is how
# `(*@KEY@:DESCRIPTION*)` ended up parsed as name='DESCRIPTION' body='*'.
# ``@KEY@`` carries an extra ``@`` so `KEY` must not be matched from inside it,
# which the mandatory ``@KEY@`` anchor ensures.
_KEY_RE = re.compile(
    r"\(\*@KEY@\s*:?\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<body>.*?)\*\)",
    re.DOTALL,
)
# Any `(*@KEY@ ... *)` comment, used to delimit regions.
_KEY_SPLIT_RE = re.compile(r"\(\*@KEY@[^*]*\*\)")


def read_text(path: str | Path) -> str:
    """Read a text file, tolerating the mixed encodings MotionWorks emits."""

    p = Path(path)
    raw = p.read_bytes()
    if not raw:
        return ""
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: never fail a tool call over an encoding quirk.
    return raw.decode("utf-8", errors="replace")


def strip_html(text: str) -> str:
    """Unwrap an HTML-wrapped ST body into plain IEC source.

    The PLCopen export wraps ST in XHTML. The text nodes already contain the
    real newlines, so we only need to drop ``<br/>`` and any residual markup —
    we must NOT re-indent, because tab alignment in the source is meaningful
    (it is how the original author lined up ``:=`` and inline comments).
    """

    if not text:
        return ""
    out = _BR_RE.sub("\n", text)
    # Any other element (rare) is dropped but its inner text kept.
    out = _TAG_RE.sub("", out)
    out = html.unescape(out)
    return out.strip("\n")


def parse_directives(text: str) -> dict[str, str]:
    """Parse the ``(*@PROPERTIES_EX@ ... *)`` header of an Extended-IEC file."""

    m = _PROPERTIES_RE.search(text)
    if not m:
        return {}
    props: dict[str, str] = {}
    for line in m.group("body").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        props[key.strip().upper()] = value.strip()
    return props


def extract_key_block(text: str, key: str) -> str:
    """Return the content of a ``(*@KEY@:KEY ... *)`` region.

    The region's extent is the *next comment marker*, not the inside of the key
    comment itself. The writer emits the key as a bare ``(*@KEY@:NAME*)`` with no
    body, so the content lives between the key comment and the following one —
    ``(*@KEY@:DESCRIPTION*)`` … ``(*@KEY@:END_DESCRIPTION*)``.
    """

    target = key.upper()
    for m in _KEY_SPLIT_RE.finditer(text):
        marker = m.group(0)
        name_match = _KEY_RE.match(marker)
        if not name_match:
            continue
        if name_match.group("name").upper() != target:
            continue
        # Content runs from here to the next comment marker.
        nxt = _KEY_SPLIT_RE.search(text, m.end())
        end = nxt.start() if nxt else len(text)
        return text[m.end() : end]
    return ""


def extract_description(text: str) -> str:
    """Pull the POU description out of the DESCRIPTION region."""

    return extract_key_block(text, "DESCRIPTION").strip()


def split_on_key_markers(text: str) -> list[tuple[str, str]]:
    """Split a file into ``[(marker, segment), ...]`` at every ``(*@KEY@...*)``.

    The marker is the raw comment, so callers can decide what each region means.
    """

    parts: list[tuple[str, str]] = []
    last = 0
    for m in _KEY_SPLIT_RE.finditer(text):
        if m.start() > last:
            parts.append(("", text[last:m.start()]))
        parts.append((m.group(0), ""))
        last = m.end()
    if last < len(text):
        parts.append(("", text[last:]))
    return [(mk, seg) for mk, seg in parts if mk or seg.strip()]


_IDENT_START = r"[A-Za-z_]"
_IDENT_CHAR = r"[A-Za-z0-9_]"
QUALIFIED_IDENT_RE = re.compile(rf"\b({_IDENT_START}{_IDENT_CHAR}*(?:\.{_IDENT_START}{_IDENT_CHAR}*)*)\b")

# IEC keywords/type names we exclude from cross-reference symbol extraction.
IEC_KEYWORDS = frozenset("""
    PROGRAM END_PROGRAM FUNCTION FUNCTION_BLOCK END_FUNCTION_BLOCK END_FUNCTION
    VAR VAR_INPUT VAR_OUTPUT VAR_IN_OUT VAR_EXTERNAL VAR_GLOBAL VAR_TEMP
    VAR_RETAIN END_VAR RETAIN PERSISTENT CONSTANT AT TYPE END_TYPE STRUCT
    END_STRUCT ARRAY OF IF THEN ELSIF ELSE END_IF CASE END_CASE FOR TO BY DO
    END_FOR WHILE END_WHILE REPEAT UNTIL END_REPEAT EXIT RETURN CONTINUE
    TRUE FALSE AND OR XOR NOT MOD DIV
    BOOL SINT INT DINT LINT USINT UINT UDINT ULINT BYTE WORD DWORD LWORD
    REAL LREAL STRING WSTRING TIME DATE TOD DT TIME_OF_DAY DATE_AND_TIME
    ANY ANY_NUM ANY_INT ANY_REAL ANY_BIT ANY_STRING POINTER REF_TO
""".split())


def normalize_ws(text: str) -> str:
    """Collapse runs of whitespace — used for comparing rendered logic."""

    return re.sub(r"\s+", " ", text or "").strip()
