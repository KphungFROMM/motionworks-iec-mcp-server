"""The vendor firmware library reference, extracted from MotionWorks' own help.

No MotionWorks *export* defines the firmware motion library. `MC_Power` appears in
a PLCopen export only as ``<block typeName="MC_Power">``; nothing states that it
takes ten parameters, what their types are, which are unsupported on this
firmware, or what they mean. That is the difference between code that compiles and
code that merely looks plausible.

The reference does exist, though. MotionWorks installs it as compiled help::

    C:\\ProgramData\\Yaskawa\\MotionWorks IEC 3 Pro\\<version>\\plc\\FW_LIB\\<lib>\\*.chm

Windows' built-in decompiler turns each ``.chm`` into ordinary HTML::

    hh.exe -decompile <dir> <chm>

and the pages are regular enough to parse mechanically. Verified against the
installed 3.7.5.1 reference, one library's help yields **106 function-block pages**
(46 ``MC_*``, 60 ``Y_*``), **35 data-type pages** and a full enumerated-type listing.

Page shapes, all located by table class rather than by scraping text:

``table.fb_parameters``
    A block. ``tr`` rows are either a scope banner (``td.group`` =
    ``VAR_IN_OUT`` / ``VAR_INPUT`` / ``VAR_OUTPUT``) or a parameter: PLCopen
    requirement level, name, data type, description, default. ``tr.unsupported``
    marks a parameter this firmware does not implement — worth surfacing, because
    the block still accepts it.

``table.fb_datatypes``
    A data type: ``*``, Element, Data Type, Description, Usage. The same class
    carries enumerated types, where rows alternate between an enum-name row and
    numeric value rows.

Nothing here is committed to the repository: extraction goes to a local cache
directory, and the Yaskawa content stays where Yaskawa put it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .util import read_text

# Bump when the parsing logic changes, so a stale catalog is not reused.
CATALOG_SCHEMA = 3

# Libraries whose help is worth extracting by default. The motion reference is one
# shared 19 MB CHM that PLCopenPlus, YMotion and YCoordinatedMotion each ship a
# copy of (confirmed by hash), so extracting "PLCopenPlus" alone yields all three.
DEFAULT_LIBRARIES = (
    "PLCopenPlus_v_2_2a",
    "YMotion",
    "YCoordinatedMotion",
    "YAxsGrp",
)

# Libraries that are not motion-related; extracted only when asked for.
AUXILIARY_LIBRARIES = (
    "BIT_UTIL",
    "NVTCPUDP",
    "PROCONOS",
    "LegacyProConOS",
    "YDeviceComm",
    "YIODrv",
    "YMLinkIO",
)


# --------------------------------------------------------------------------
# Data model (all JSON-serialisable)
# --------------------------------------------------------------------------


@dataclass
class LibPin:
    """One formal parameter of a library function block."""

    name: str
    type: str = ""
    scope: str = ""
    default: str = ""
    description: str = ""
    supported: bool = True
    level: str = ""          # PLCopen requirement level: B / E / V

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "direction": _direction(self.scope),
        }
        if self.default:
            d["default"] = self.default
        if self.description:
            d["description"] = self.description
        if not self.supported:
            d["supported"] = False
        if self.level:
            d["level"] = self.level
        return d


def _direction(scope: str) -> str:
    return {
        "VAR_INPUT": "input",
        "VAR_OUTPUT": "output",
        "VAR_IN_OUT": "inOut",
        "VAR": "local",
    }.get(scope, scope.lower() or "unknown")


@dataclass
class LibBlock:
    """A function block from the firmware reference."""

    name: str
    library: str = ""
    description: str = ""
    category: str = ""
    pins: list[LibPin] = field(default_factory=list)
    notes: str = ""
    example: str = ""
    error_description: str = ""
    related: list[str] = field(default_factory=list)
    page: str = ""

    @property
    def inout_pins(self) -> list[LibPin]:
        return [p for p in self.pins if p.scope == "VAR_IN_OUT"]

    @property
    def input_pins(self) -> list[LibPin]:
        return [p for p in self.pins if p.scope == "VAR_INPUT"]

    @property
    def output_pins(self) -> list[LibPin]:
        return [p for p in self.pins if p.scope == "VAR_OUTPUT"]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "source": "firmware library help",
            "pinCount": len(self.pins),
            "pins": [p.to_dict() for p in self.pins],
        }
        if self.library:
            d["library"] = self.library
        if self.description:
            d["description"] = self.description
        if self.example:
            d["example"] = self.example
        if self.notes:
            d["notes"] = self.notes
        if self.error_description:
            d["errorDescription"] = self.error_description
        if self.related:
            d["relatedBlocks"] = self.related
        unsupported = [p.name for p in self.pins if not p.supported]
        if unsupported:
            d["unsupportedPins"] = unsupported
        return d


@dataclass
class LibMember:
    """A member of a library data type."""

    name: str
    type: str = ""
    description: str = ""
    usage: str = ""
    level: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.description:
            d["description"] = self.description
        if self.usage:
            d["usage"] = self.usage
        return d


@dataclass
class LibType:
    """A data type or enumerated type from the firmware reference."""

    name: str
    kind: str = "struct"          # struct | enum | other
    description: str = ""
    declaration: str = ""
    members: list[LibMember] = field(default_factory=list)
    values: list[dict[str, str]] = field(default_factory=list)
    example: str = ""
    page: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "source": "firmware library help",
        }
        if self.description:
            d["description"] = self.description
        if self.declaration:
            d["declaration"] = self.declaration
        if self.members:
            d["members"] = [m.to_dict() for m in self.members]
            d["memberCount"] = len(self.members)
        if self.values:
            d["enumValues"] = self.values
            d["valueCount"] = len(self.values)
        if self.example:
            d["example"] = self.example
        return d


@dataclass
class Catalog:
    """The parsed reference for one installed MotionWorks version."""

    version: str = ""
    root: str = ""
    libraries: list[str] = field(default_factory=list)
    blocks: dict[str, LibBlock] = field(default_factory=dict)
    types: dict[str, LibType] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    # -- lookups -----------------------------------------------------------

    def block(self, name: str) -> LibBlock | None:
        """Case-insensitive block lookup, tolerating an instance suffix."""

        if name in self.blocks:
            return self.blocks[name]
        lowered = name.lower()
        for key, value in self.blocks.items():
            if key.lower() == lowered:
                return value
        return None

    def data_type(self, name: str) -> LibType | None:
        if name in self.types:
            return self.types[name]
        lowered = name.lower()
        for key, value in self.types.items():
            if key.lower() == lowered:
                return value
        return None

    def summary(self) -> dict[str, Any]:
        by_library: dict[str, int] = {}
        for block in self.blocks.values():
            by_library[block.library] = by_library.get(block.library, 0) + 1
        return {
            "version": self.version,
            "root": self.root,
            "libraries": self.libraries,
            "blockCount": len(self.blocks),
            "typeCount": len(self.types),
            "enumTypeCount": sum(1 for t in self.types.values() if t.kind == "enum"),
            "enumValueCount": sum(len(t.values) for t in self.types.values()),
            "blocksByLibrary": by_library,
            "errors": self.errors[:10],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CATALOG_SCHEMA,
            "version": self.version,
            "root": self.root,
            "libraries": self.libraries,
            "blocks": [b.to_dict() for b in self.blocks.values()],
            "types": [t.to_dict() for t in self.types.values()],
            "errors": self.errors,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "Catalog":
        catalog = Catalog(
            version=payload.get("version", ""),
            root=payload.get("root", ""),
            libraries=list(payload.get("libraries", [])),
            errors=list(payload.get("errors", [])),
        )
        for b in payload.get("blocks", []):
            pins = [
                LibPin(
                    name=p.get("name", ""),
                    type=p.get("type", ""),
                    scope=_scope_from_direction(p.get("direction", "")),
                    default=p.get("default", ""),
                    description=p.get("description", ""),
                    supported=p.get("supported", True),
                    level=p.get("level", ""),
                )
                for p in b.get("pins", [])
            ]
            catalog.blocks[b["name"]] = LibBlock(
                name=b["name"],
                library=b.get("library", ""),
                description=b.get("description", ""),
                pins=pins,
                notes=b.get("notes", ""),
                example=b.get("example", ""),
                error_description=b.get("errorDescription", ""),
                related=list(b.get("relatedBlocks", [])),
                page=b.get("page", ""),
            )
        for t in payload.get("types", []):
            catalog.types[t["name"]] = LibType(
                name=t["name"],
                kind=t.get("kind", "struct"),
                description=t.get("description", ""),
                declaration=t.get("declaration", ""),
                members=[
                    LibMember(
                        name=m.get("name", ""),
                        type=m.get("type", ""),
                        description=m.get("description", ""),
                        usage=m.get("usage", ""),
                    )
                    for m in t.get("members", [])
                ],
                values=list(t.get("enumValues", [])),
                example=t.get("example", ""),
            )
        return catalog


def _scope_from_direction(direction: str) -> str:
    return {
        "input": "VAR_INPUT",
        "output": "VAR_OUTPUT",
        "inOut": "VAR_IN_OUT",
        "local": "VAR",
    }.get(direction, "")


# --------------------------------------------------------------------------
# Locating the installed reference
# --------------------------------------------------------------------------


def default_cache_dir() -> Path:
    """Where extracted pages and the parsed catalog are kept."""

    override = os.environ.get("MOTIONWORKS_FWLIB_CACHE")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "motionworks-iec-mcp" / "fwlib"
    return Path.home() / ".cache" / "motionworks-iec-mcp" / "fwlib"


def default_install_dirs() -> list[Path]:
    """Candidate ``FW_LIB`` directories, newest MotionWorks version first."""

    roots: list[Path] = []
    override = os.environ.get("MOTIONWORKS_FWLIB")
    if override:
        roots.append(Path(override))

    bases = [
        Path(r"C:\ProgramData\Yaskawa\MotionWorks IEC 3 Pro"),
        Path(r"C:\ProgramData\Yaskawa\MotionWorks IEC 3 Express"),
        Path(r"C:\Program Files (x86)\Yaskawa\MotionWorks IEC 3 Pro"),
        Path(r"C:\Program Files\Yaskawa\MotionWorks IEC 3 Pro"),
    ]
    for base in bases:
        if not base.is_dir():
            continue
        try:
            versions = sorted(
                (p for p in base.iterdir() if p.is_dir()),
                key=lambda p: _version_key(p.name),
                reverse=True,
            )
        except OSError:
            continue
        for version in versions:
            fw = version / "plc" / "FW_LIB"
            if fw.is_dir():
                roots.append(fw)
        fw = base / "plc" / "FW_LIB"
        if fw.is_dir():
            roots.append(fw)
    return roots


def _version_key(name: str) -> tuple:
    """Sort MotionWorks version folder names numerically (``3_7_5_1_667``)."""

    parts = []
    for chunk in name.replace("-", "_").split("_"):
        parts.append(int(chunk) if chunk.isdigit() else -1)
    return tuple(parts)


def find_install() -> Path | None:
    """The ``FW_LIB`` directory of the newest installed MotionWorks, if present.

    An explicit ``MOTIONWORKS_FWLIB`` is authoritative: if it is set but does not
    exist, this returns ``None`` rather than quietly falling back to some other
    installation. Silently ignoring a mistyped path would make the reference look
    broken for no discoverable reason.
    """

    override = os.environ.get("MOTIONWORKS_FWLIB")
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None

    for candidate in default_install_dirs():
        if candidate.is_dir():
            return candidate
    return None


def install_version(fw_lib: Path) -> str:
    """The version string for a ``FW_LIB`` path (its grandparent folder name)."""

    for parent in (fw_lib, fw_lib.parent, fw_lib.parent.parent):
        if parent.name and any(ch.isdigit() for ch in parent.name):
            return parent.name
    return fw_lib.name


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def unique_chms(fw_lib: Path) -> dict[str, tuple[str, Path]]:
    """``{sha: (library, chm)}`` for every distinct help file under ``FW_LIB``.

    The motion reference is one 19 MB file that ``PLCopenPlus_v_2_2a``,
    ``YMotion`` and ``YCoordinatedMotion`` each ship a copy of, so de-duplicating
    by content hash avoids decompiling the same 40 MB three times.
    """

    out: dict[str, tuple[str, Path]] = {}
    try:
        chms = sorted(fw_lib.rglob("*.chm"))
    except OSError:
        return out
    for chm in chms:
        try:
            digest = hashlib.sha256(chm.read_bytes()).hexdigest()[:16]
        except OSError:
            continue
        library = chm.parent.name
        out.setdefault(digest, (library, chm))
    return out


def have_decompiler() -> bool:
    """Can we decompile the help files?

    Needs ``hh.exe`` *and* PowerShell, for the reason in :func:`_run_decompiler`.
    """

    if sys.platform != "win32":
        return False
    if shutil.which("hh.exe") is None:
        return False
    return shutil.which("powershell") is not None or shutil.which("pwsh") is not None


def _run_decompiler(chm: Path, dest: Path, timeout: int) -> tuple[bool, str]:
    """Invoke ``hh.exe -decompile`` the only way that actually works.

    Spawning ``hh.exe`` directly does *not* work: it exits 0 immediately and writes
    nothing. Verified against four direct-spawn shapes from Python —
    plain argv, ``shell=True``, ``cmd /c``, and ``CREATE_NO_WINDOW`` — all of which
    returned 0 with zero pages, while PowerShell's ``Start-Process -Wait`` on the
    same file produced the full page tree.

    ``Start-Process`` uses ShellExecute semantics, which is evidently what this GUI
    executable needs; ``ShellExecuteExW`` via ctypes was tried as a dependency-free
    alternative and also produced nothing. So PowerShell it is, and ``MOTIONWORKS_FWLIB``
    pointing at pre-extracted HTML remains the escape hatch for locked-down hosts.
    """

    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        return False, "neither powershell nor pwsh is on PATH, needed to drive hh.exe"

    def quote(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = (
        f"Start-Process -FilePath hh.exe -ArgumentList '-decompile',{quote(dest)},{quote(chm)}"
        " -Wait -WindowStyle Hidden"
    )
    try:
        subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return True, ""
    except subprocess.TimeoutExpired:
        # Not fatal: hh.exe usually has finished writing by now.
        return True, "hh.exe timed out; continuing with whatever was extracted"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"failed to run hh.exe: {exc}"


def decompile(chm: Path, dest: Path, timeout: int = 600, settle: int = 120) -> tuple[bool, str]:
    """Extract one ``.chm`` into ``dest``. Idempotent via a marker file."""

    marker = dest / ".extracted"
    if marker.exists():
        return True, "cached"
    if not have_decompiler():
        return False, (
            "cannot decompile the firmware library help: needs hh.exe and "
            "PowerShell (Windows). Set MOTIONWORKS_FWLIB to a folder of "
            "already-extracted .htm pages instead."
        )
    dest.mkdir(parents=True, exist_ok=True)

    def pages() -> int:
        try:
            return sum(1 for _ in dest.rglob("*.htm"))
        except OSError:
            return 0

    ok, detail = _run_decompiler(chm, dest, timeout)

    # hh.exe does not necessarily finish writing when it returns, and it reports
    # success whether or not it extracted anything, so success is judged by pages.
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        if pages():
            break
        time.sleep(0.5)

    produced = pages()
    if not produced:
        reasons = []
        if not ok:
            reasons.append(detail)
        elif detail:
            reasons.append(detail)
        reasons.append(f"hh.exe produced no pages for {chm.name}")
        return False, "; ".join(reasons)

    marker.write_text(f"{CATALOG_SCHEMA}\n", encoding="utf-8")
    if detail:
        return True, f"{produced} pages ({detail})"
    return True, f"{produced} pages"


def extracted_root(fw_lib: Path | None = None, cache_dir: Path | None = None) -> Path | None:
    """Extract the help files for the libraries we care about; return their root.

    Lazy by design: only the libraries named in ``libraries`` are decompiled, so a
    project that uses only the motion reference never pays for the auxiliary
    libraries' help.
    """

    fw_lib = fw_lib or find_install()
    if fw_lib is None:
        return None
    cache = (cache_dir or default_cache_dir()) / install_version(fw_lib)
    return cache


def ensure_extracted(
    libraries: Iterable[str] | None = None,
    fw_lib: Path | None = None,
    cache_dir: Path | None = None,
) -> tuple[Path | None, list[str]]:
    """Make sure the named libraries' pages are on disk.

    Returns ``(page_root, problems)``. ``page_root`` is ``None`` when nothing could
    be extracted, and ``problems`` explains why in terms a user can act on.
    """

    fw_lib = fw_lib or find_install()
    if fw_lib is None:
        return None, ["no MotionWorks installation with an FW_LIB folder was found"]

    wanted = {lib.lower() for lib in (libraries or DEFAULT_LIBRARIES)}
    chms = unique_chms(fw_lib)
    if not chms:
        return None, [f"no .chm help files under {fw_lib}"]

    root = extracted_root(fw_lib, cache_dir)
    assert root is not None
    problems: list[str] = []
    extracted_any = False

    for digest, (library, chm) in sorted(chms.items(), key=lambda kv: kv[1][0]):
        # Extract a library's help when it is asked for, or when it is the shared
        # motion reference (recognised by shipping from several library folders).
        shared = False
        if library.lower() not in wanted:
            same = [lib for d, (lib, _c) in chms.items() if d == digest]
            shared = len(same) > 1
            if not shared:
                continue
        dest = root / digest
        ok, detail = decompile(chm, dest)
        if ok:
            extracted_any = True
        else:
            problems.append(detail)

    if not extracted_any:
        return None, problems or ["nothing was extracted"]
    return root, problems


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split())


def _cells(row) -> list[str]:
    return [_text(c) for c in row if _local(c.tag) in ("td", "th")]


def _tables(root, cls: str | None = None) -> list:
    out = []
    for el in root.iter():
        if _local(el.tag) != "table":
            continue
        if cls is None or el.get("class") == cls:
            out.append(el)
    return out


def _rows(table) -> list:
    return [c for c in table if _local(c.tag) == "tr"]


def _parents(root) -> dict:
    """Map each element to its parent, so "is this inside a table?" is answerable."""

    out: dict = {}
    for parent in root.iter():
        for child in parent:
            out[child] = parent
    return out


def _inside_table(el, parents: dict) -> bool:
    node = parents.get(el)
    while node is not None:
        if _local(node.tag) == "table":
            return True
        node = parents.get(node)
    return False


def _sections(root) -> dict[str, str]:
    """Map top-level ``<h2>`` heading text to the content that follows it.

    Block pages are organised as ``Library``, ``Parameters``, ``Notes``,
    ``Related Function Blocks``, ``Error Description`` and ``Example``. The notes
    and example text is genuinely useful to an agent writing a call.

    Headings inside the banner table are ignored: the banner re-states the block
    name in an ``<h2>``, and treating that as a section heading truncates every
    section at the top of the page.
    """

    parents = _parents(root)
    out: dict[str, str] = {}
    current: str | None = None
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "h2":
            if _inside_table(el, parents):
                continue
            current = _text(el).strip()
            out.setdefault(current, "")
            continue
        if current is None:
            continue
        if tag in ("p", "li", "pre") and not _inside_table(el, parents):
            text = _text(el)
            if text and text not in out[current]:
                out[current] = (out[current] + " " + text).strip()
    return out


def _document_title(root, page: str = "") -> str:
    """The subject of a help page: the block or type name.

    Taken from ``<title>``. The page's visible title is re-drawn inside the banner
    table, so searching for the first ``<h2>`` finds a *section* heading
    ("Library", "Parameters") rather than the subject. ``<span class="SystemTitle">``
    is the fallback, then the file name.
    """

    for el in root.iter():
        if _local(el.tag) == "title":
            text = _text(el).strip()
            if text:
                return text
    for el in root.iter():
        if _local(el.tag) == "span" and "SystemTitle" in (el.get("class") or ""):
            text = _text(el).strip()
            if text:
                return text
    if page:
        return Path(page).stem.replace("Data_Type__", "")
    return ""


def parse_block_page(raw: bytes, page: str = "") -> LibBlock | None:
    """Parse one ``table.fb_parameters`` page into a :class:`LibBlock`."""

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    tables = _tables(root, "fb_parameters")
    if not tables:
        return None

    parents = _parents(root)
    name = _document_title(root, page)
    if not name:
        return None
    # Overview pages ("Function Block List", "Data Types", ...) also carry
    # parameter-shaped tables. A real block name is a single identifier token.
    if not _is_identifier(name):
        return None

    block = LibBlock(name=name, page=page)
    sections = _sections(root)
    block.library = sections.get("Library", "").strip()
    block.description = _lead_description(root, parents)
    block.error_description = sections.get("Error Description", "").strip()
    block.notes = sections.get("Notes", "").strip()
    block.example = sections.get("Example", "").strip()

    related_heading = next(
        (k for k in sections if k.lower().startswith("related")), None
    )
    if related_heading:
        seen_related: list[str] = []
        for word in sections[related_heading].replace(",", " ").split():
            token = word.strip(".;:()")
            if _is_identifier(token) and _looks_like_block_name(token):
                if token not in seen_related:
                    seen_related.append(token)
        block.related = seen_related[:20]

    scope = ""
    for row in _rows(tables[0]):
        cells = _cells(row)
        if not cells:
            continue
        # A scope banner may span one cell (`VAR_IN_OUT`) or two
        # (`VAR_INPUT` + a `Default` column label). Matching only the single-cell
        # form left every input pin mislabelled as inOut.
        if cells[0].upper().startswith("VAR"):
            scope = cells[0].upper()
            continue
        if cells[0] == "Parameter" or cells[0].startswith("PLCopen requirement"):
            continue
        if len(cells) < 3 or not scope:
            continue
        level, pin_name, pin_type = cells[0], cells[1], cells[2]
        if not pin_name or pin_name.lower() == "parameter":
            continue
        description = cells[3] if len(cells) > 3 else ""
        default = cells[4] if len(cells) > 4 else ""
        supported = "unsupported" not in (row.get("class") or "").lower()
        block.pins.append(
            LibPin(
                name=pin_name,
                type=pin_type,
                scope=scope,
                default=default,
                description=description,
                supported=supported,
                level=level,
            )
        )

    if not block.pins:
        return None
    return block


def _is_identifier(text: str) -> bool:
    """Is this a bare IEC identifier (no spaces, not a sentence)?"""

    if not text or len(text) > 64:
        return False
    if not (text[0].isalpha() or text[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in text)


def _lead_description(root, parents: dict) -> str:
    """The intro sentence(s) before the first section heading.

    Section headings are the top-level ``<h2>`` elements; the page's own title is
    drawn inside the banner table and is not one of them.
    """

    chunks: list[str] = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "h2" and not _inside_table(el, parents):
            break
        if tag != "p" or _inside_table(el, parents):
            continue
        text = _text(el)
        if text and not text.startswith(("PLCopen Help", "Help version")):
            chunks.append(text)
    return " ".join(chunks).strip()


def _looks_like_block_name(word: str) -> bool:
    return (
        word.startswith(("MC_", "Y_"))
        or word[0].isupper() and any(c.islower() for c in word)
    ) and len(word) > 3


def parse_type_page(raw: bytes, page: str = "") -> LibType | None:
    """Parse one ``table.fb_datatypes`` page into a :class:`LibType`."""

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    tables = _tables(root, "fb_datatypes")
    if not tables:
        return None

    name = _document_title(root, page)
    if name.upper().startswith("DATATYPE:"):
        name = name.split(":", 1)[1].strip()
    if not name:
        return None

    tp = LibType(name=name, page=page)
    sections = _sections(root)
    tp.example = sections.get("Example", "").strip()

    table = tables[0]
    rows = _rows(table)
    header = _cells(rows[0]) if rows else []

    # An enumerated type: a title row, then numeric value rows.
    if any("Enum" in h for h in header):
        tp.kind = "enum"
        for row in rows[1:]:
            cells = _cells(row)
            if not cells:
                continue
            if len(cells) >= 2 and not cells[0].isdigit():
                # `<EnumName>`, `<description>`
                tp.description = (cells[1] if len(cells) > 1 else "").strip()
                continue
            if len(cells) >= 2 and cells[0].isdigit():
                tp.values.append({
                    "value": cells[0],
                    "name": cells[1],
                    "description": cells[2] if len(cells) > 2 else "",
                })
        return tp

    # A structured type: `*`, Element, Data Type, Description, Usage.
    tp.kind = "struct"
    for row in rows[1:]:
        cells = _cells(row)
        if len(cells) < 3:
            continue
        level, member, member_type = cells[0], cells[1], cells[2]
        description = cells[3] if len(cells) > 3 else ""
        usage = cells[4] if len(cells) > 4 else ""
        if not member:
            # A header-ish row carrying the declaration in the type column.
            if member_type:
                tp.declaration = (tp.declaration + " " + member_type).strip()
            continue
        if member.upper() in ("ELEMENT", "DATA TYPE"):
            continue
        tp.members.append(
            LibMember(name=member, type=member_type, description=description,
                      usage=usage, level=level)
        )
    if not tp.members and not tp.declaration:
        return None
    return tp


def parse_enum_index(raw: bytes, page: str = "") -> list[LibType]:
    """Parse the aggregate ``Enumerated_Types`` page into many enum types.

    Rows alternate: a name row (one populated cell) starts a type, and its numeric
    value rows follow until the next name row.
    """

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    tables = _tables(root, "fb_datatypes")
    if not tables:
        return []

    out: list[LibType] = []
    current: LibType | None = None
    for row in _rows(tables[0]):
        cells = _cells(row)
        if not cells:
            continue
        if cells[0].isdigit() and len(cells) >= 2:
            if current is None:
                continue
            current.values.append({
                "value": cells[0],
                "name": cells[1],
                "description": cells[2] if len(cells) > 2 else "",
            })
            continue
        name = cells[0].strip()
        if not name:
            continue
        # A name row may carry a following description cell.
        current = LibType(
            name=name,
            kind="enum",
            description=(cells[1] if len(cells) > 1 else "").strip(),
            page=page,
        )
        out.append(current)
    return [t for t in out if t.values]


def _merge_values(existing: list[dict[str, str]],
                  incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    """Union two enum value lists by name, preferring a described entry.

    An enumerated type is described both on its own page and in the aggregate
    ``Enumerated_Types`` page, and the two do not always list the same members.
    Overwriting with whichever was parsed last lost values, so they are merged.
    """

    by_name: dict[str, dict[str, str]] = {}
    for value in list(existing) + list(incoming):
        name = value.get("name", "")
        if not name:
            continue
        prior = by_name.get(name)
        if prior is None:
            by_name[name] = dict(value)
            continue
        # Keep the richer description.
        if not prior.get("description") and value.get("description"):
            prior["description"] = value["description"]
    def sort_key(item: dict[str, str]) -> tuple:
        raw = item.get("value", "")
        return (int(raw) if raw.lstrip("-").isdigit() else 10**6, raw)
    return sorted(by_name.values(), key=sort_key)


def parse_tree(root_dir: Path) -> Catalog:
    """Parse an extracted help tree into a :class:`Catalog`."""

    catalog = Catalog(root=str(root_dir))

    pages = sorted(root_dir.rglob("*.htm"))
    for page in pages:
        try:
            raw = page.read_bytes()
        except OSError:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            catalog.errors.append(f"unparsable page: {page.name}")
            continue

        classes = {t.get("class") for t in root.iter() if _local(t.tag) == "table"}
        stem = page.stem

        if "Enumerated_Types" in stem:
            for tp in parse_enum_index(raw, str(page)):
                target = catalog.types.get(tp.name)
                if target is None:
                    catalog.types[tp.name] = tp
                else:
                    target.values = _merge_values(target.values, tp.values)
                    if not target.description:
                        target.description = tp.description
            continue

        if "fb_parameters" in classes:
            block = parse_block_page(raw, str(page))
            if block is not None:
                existing = catalog.blocks.get(block.name)
                # A block described twice (per-library duplicates) keeps the
                # richest record rather than the last one read.
                if existing is None or len(block.pins) > len(existing.pins):
                    catalog.blocks[block.name] = block
            continue

        if "fb_datatypes" in classes:
            tp = parse_type_page(raw, str(page))
            if tp is not None:
                current = catalog.types.get(tp.name)
                if current is None:
                    catalog.types[tp.name] = tp
                else:
                    if tp.members and not current.members:
                        current.members = tp.members
                        current.declaration = tp.declaration
                    if tp.values:
                        current.kind = "enum"
                        current.values = _merge_values(current.values, tp.values)
                    if not current.description and tp.description:
                        current.description = tp.description
                    if not current.example and tp.example:
                        current.example = tp.example

    # Keep only "related blocks" that actually exist in the reference; the
    # surrounding prose is full of capitalised words that look like names.
    known = set(catalog.blocks)
    for block in catalog.blocks.values():
        if block.related:
            block.related = [name for name in block.related if name in known][:12]

    catalog.libraries = sorted({b.library for b in catalog.blocks.values() if b.library})
    return catalog


# --------------------------------------------------------------------------
# Loading, with cache
# --------------------------------------------------------------------------


def _cache_file(root: Path) -> Path:
    return root / f"catalog_v{CATALOG_SCHEMA}.json"


def load_catalog(
    fw_lib: Path | None = None,
    cache_dir: Path | None = None,
    libraries: Iterable[str] | None = None,
    refresh: bool = False,
) -> tuple[Catalog | None, list[str]]:
    """Return the parsed firmware library catalog, extracting it once.

    Returns ``(catalog, problems)``. ``catalog`` is ``None`` when the reference is
    unavailable, and ``problems`` says why — the tool layer turns that into an
    actionable message rather than an empty result.
    """

    fw_lib = fw_lib or find_install()
    if fw_lib is None:
        return None, [
            "no MotionWorks installation was found (looked for "
            "C:\\ProgramData\\Yaskawa\\MotionWorks IEC 3 Pro\\<version>\\plc\\FW_LIB); "
            "set MOTIONWORKS_FWLIB to an FW_LIB folder or a folder of extracted HTML"
        ]

    # An override may point straight at an already-extracted tree of HTML pages.
    if fw_lib.is_dir() and not any(fw_lib.rglob("*.chm")) and any(fw_lib.rglob("*.htm")):
        catalog, problems = _from_haystack(fw_lib, cache_dir, refresh)
        if catalog is not None:
            return catalog, problems
        return None, problems

    root, problems = ensure_extracted(libraries, fw_lib, cache_dir)
    if root is None:
        return None, problems

    catalog, more = _from_haystack(root, cache_dir, refresh, version=install_version(fw_lib))
    problems.extend(more)
    if catalog is None:
        return None, problems or ["no function-block pages were found in the extracted help"]
    return catalog, problems


def _from_haystack(
    root: Path,
    cache_dir: Path | None,
    refresh: bool,
    version: str = "",
) -> tuple[Catalog | None, list[str]]:
    """Parse (or reuse a cached parse of) everything under ``root``."""

    cache_file = _cache_file(root)
    if cache_file.exists() and not refresh:
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if payload.get("schema") == CATALOG_SCHEMA:
                catalog = Catalog.from_dict(payload)
                if catalog.blocks:
                    return catalog, []
        except (OSError, ValueError):
            pass    # fall through to a fresh parse

    catalog = parse_tree(root)
    if version:
        catalog.version = version
    if not catalog.blocks:
        # A root that contains only subdirectories of extracted libraries: parse
        # each child and merge.
        merged = Catalog(version=version, root=str(root))
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if child.name.startswith("."):
                continue
            sub = parse_tree(child)
            for name, block in sub.blocks.items():
                existing = merged.blocks.get(name)
                if existing is None or len(block.pins) > len(existing.pins):
                    merged.blocks[name] = block
            for name, tp in sub.types.items():
                if name not in merged.types or (
                    tp.members and not merged.types[name].members
                ):
                    merged.types[name] = tp
            merged.errors.extend(sub.errors)
        if version:
            merged.version = version
        merged.libraries = sorted(
            {b.library for b in merged.blocks.values() if b.library}
        )
        catalog = merged

    if not catalog.blocks:
        return None, list(catalog.errors[:5]) or [f"no block pages found under {root}"]

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(catalog.to_json(), encoding="utf-8")
    except OSError:
        pass    # a cache we cannot write is not a failure

    return catalog, list(catalog.errors[:5])


# --------------------------------------------------------------------------
# Convenience for the tool layer
# --------------------------------------------------------------------------

_CATALOG_CACHE: dict[str, tuple[Catalog | None, list[str]]] = {}


def catalog_for(
    libraries: Iterable[str] | None = None,
    fw_lib: Path | None = None,
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> tuple[Catalog | None, list[str]]:
    """Process-lifetime memoised :func:`load_catalog`.

    The key includes the *resolved* install path, not just the arguments: two
    callers that pass no path but sit behind different ``MOTIONWORKS_FWLIB`` values
    must not share an entry.
    """

    resolved = fw_lib or find_install()
    key = (
        f"{resolved or ''}|{cache_dir or ''}|"
        f"{','.join(sorted(libraries or DEFAULT_LIBRARIES))}"
    )
    if refresh or key not in _CATALOG_CACHE:
        _CATALOG_CACHE[key] = load_catalog(
            fw_lib=fw_lib, cache_dir=cache_dir, libraries=libraries, refresh=refresh
        )
    return _CATALOG_CACHE[key]


def clear_memo() -> None:
    _CATALOG_CACHE.clear()


# --------------------------------------------------------------------------
# Combining the reference with what a project says
# --------------------------------------------------------------------------


def merge_signature(project_signature: dict[str, Any] | None, block: LibBlock) -> dict[str, Any]:
    """Combine a project-derived signature with the firmware reference.

    The two are authoritative about different things, so neither simply wins:

    * the **project** knows the exact pin list this firmware build compiled against
      (``.DIT``) or what the code actually passes;
    * the **reference** knows data types, defaults, which pins are unimplemented,
      per-pin meaning, the block's purpose and its example.

    So the project supplies the pin set and order, the reference fills in
    everything else, and any pin the project lists that the reference does not
    know (or vice versa) is reported rather than dropped.
    """

    reference_pins = {p.name: p for p in block.pins}
    merged: list[dict[str, Any]] = []
    used: set[str] = set()

    if project_signature and project_signature.get("pins"):
        for pin in project_signature["pins"]:
            name = pin.get("name", "")
            ref = reference_pins.get(name)
            entry: dict[str, Any] = dict(pin)
            if ref is not None:
                used.add(name)
                # The vendor's own declaration of the data type is the most precise
                # statement of the block's interface (e.g. `MC_BufferMode`, not the
                # `INT` the compiler recorded underneath it), so it wins — but a
                # disagreement with the project is kept visible.
                project_type = str(entry.get("type") or "")
                if ref.type:
                    if project_type and project_type != ref.type:
                        entry["projectType"] = project_type
                    entry["type"] = ref.type
                entry.pop("typeInferred", None)
                entry["direction"] = _direction(ref.scope) or entry.get("direction")
                if ref.default:
                    entry["default"] = ref.default
                if ref.description:
                    entry["description"] = ref.description
                if not ref.supported:
                    entry["supported"] = False
            merged.append(entry)

    # Reference pins the project did not mention (common when the project has no
    # .DIT: observed usage only sees the pins it connects).
    for pin in block.pins:
        if pin.name in used:
            continue
        merged.append(pin.to_dict())

    result: dict[str, Any] = {
        "name": block.name,
        "library": block.library,
        "derivedFrom": (
            "firmware library reference"
            if not project_signature
            else f"{project_signature.get('derivedFrom', 'project')} + firmware library reference"
        ),
        "referenceAvailable": True,
        "pinCount": len(merged),
        "pins": merged,
    }
    for key in ("description", "example", "notes", "errorDescription", "relatedBlocks"):
        value = getattr(block, {
            "description": "description",
            "example": "example",
            "notes": "notes",
            "errorDescription": "error_description",
            "relatedBlocks": "related",
        }[key], None)
        if value:
            result[key] = value
    unsupported = [p["name"] for p in merged if p.get("supported") is False]
    if unsupported:
        result["unsupportedPins"] = unsupported
        result["unsupportedNote"] = (
            "these parameters exist on the block but are not implemented by this "
            "firmware; passing them has no effect"
        )
    return result


def search(catalog: Catalog, query: str, limit: int = 40) -> list[dict[str, Any]]:
    """Search blocks and types by name, description, pin name or sample code."""

    needle = (query or "").strip().lower()
    if not needle:
        return []
    hits: list[dict[str, Any]] = []

    for block in catalog.blocks.values():
        score = 0
        if needle == block.name.lower():
            score = 100
        elif needle in block.name.lower():
            score = 60
        elif any(needle in p.name.lower() for p in block.pins):
            score = 40
        elif needle in (block.description or "").lower():
            score = 30
        elif needle in (block.notes or "").lower() or needle in (block.example or "").lower():
            score = 20
        elif any(needle in (p.description or "").lower() for p in block.pins):
            score = 10
        if score:
            hits.append({
                "kind": "block",
                "name": block.name,
                "score": score,
                "library": block.library,
                "description": block.description,
                "pinCount": len(block.pins),
            })

    for tp in catalog.types.values():
        score = 0
        if needle == tp.name.lower():
            score = 100
        elif needle in tp.name.lower():
            score = 60
        elif any(needle == v.get("name", "").lower() for v in tp.values):
            score = 70
        elif any(needle in v.get("name", "").lower() for v in tp.values):
            score = 45
        elif needle in (tp.description or "").lower():
            score = 30
        if score:
            sample = [v["name"] for v in tp.values[:4]]
            hits.append({
                "kind": "enum" if tp.values else "type",
                "name": tp.name,
                "score": score,
                "description": tp.description,
                "values": sample,
            })

    hits.sort(key=lambda h: (-h["score"], h["name"]))
    return hits[:limit]
