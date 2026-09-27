"""Tell the caller what to export, when what they gave us cannot answer.

A native MotionWorks project (``<Proj>.mwt`` plus its folders) looks like the real
thing and is the most natural path to point at — but its code lives in a compound
binary (``src.st1``), so a native-only load can list POU *names* and tasks and
nothing else. An agent handed that will either say "no source available" or, worse,
start inventing code.

So instead of returning a hollow result, the loader returns the result *plus* an
instruction: what is missing, what it costs, and the exact steps to produce it.
The steps are transcribed from MotionWorks' own help, not guessed at.

The same applies in reverse to the two exports, which are each missing something
the other has. Pointing at only one of them is a silent loss of fidelity, so a
partial load gets a nudge too — with the reason, because "also export the other
thing" is advice nobody follows without knowing why.
"""

from __future__ import annotations

from pathlib import Path

from .model import SourceKind
from .util import read_text

EXPORT_KINDS = (SourceKind.PLCOPEN_XML.value, SourceKind.EXTENDED_IEC.value)

# Transcribed from the installed help: Help\XMLImportExport*.chm.
_PLCOPEN_STEPS = [
    "In MotionWorks, select the node in the project tree. Selecting 'Project' "
    "exports everything: all POUs (FBD, LD, IL, ST and SFC), data types, physical "
    "hardware, tasks and global variables.",
    "'File' > 'Export'. The 'Import / Export' dialog appears.",
    "Mark 'Export PLCopen xml file' and confirm.",
    "In 'Export as XML file', choose the folder and file name.",
    "Leave 'Save as type' on V1.01. Yaskawa's help is explicit: 'Please note that "
    "V0.99 has never been released officially. Thus, we strongly recommend to use "
    "always the file type according to V1.01.'",
]

_EXTENDED_STEPS = [
    "In MotionWorks, 'File' > 'Export'.",
    "In the 'Import / Export' dialog, choose the Extended IEC 61131-2 export and "
    "confirm.",
    "Choose a destination folder. The result is a folder tree per project, with "
    "plain-text .ST files, .GE worksheets, a .GVB global-variable sheet, an "
    "IOCONFIGURATION.EIO and one .EXP per task.",
]

_WHY_BOTH = (
    "The two exports are not redundant. Yaskawa's help states that the PLCopen XML "
    "mechanism 'does not handle programming system specific elements (i.e. elements "
    "which are not defined by the PLCopen XML specification, such as I/O "
    "configuration)' — so I/O configuration, task names and AT % addresses exist "
    "only in the Extended export. In the other direction, the Extended export "
    "carries no data-type definitions at all, so block interfaces and full graphical "
    "pin values come from the XML."
)

# What a native-only load genuinely can and cannot answer. Being precise here is the
# whole point: the agent needs to know whether to stop or to keep going.
_NATIVE_ONLY_CANNOT = [
    "any POU source code (the bodies are in a compound binary, src.st1)",
    "variables, data types or enumerated values",
    "I/O configuration",
    "function-block interfaces, unless .DIT metadata survives in the project",
]

_NATIVE_ONLY_CAN = [
    "POU names, and whether each is a program or a function block",
    "task names and the program-to-task assignment",
    "the processor and resource type",
]


def _describe(path: Path) -> dict[str, str]:
    return {"kind": "", "path": str(path)}


def _is_single_project_export(directory: Path) -> bool:
    """Is this directory *one* project's Extended export, rather than a container?

    ``extended_iec.is_extended_export`` is deliberately broad — it settles for
    ``rglob("Global_Variables.GVB")``, which is right when deciding whether a path
    can be *parsed*, because a folder holding several projects is still worth
    parsing. It is wrong when *suggesting* a path back to the user: it reports the
    workspace root and the ``Extended IEC 61131-2 Export`` container as exports,
    and pointing there merges four unrelated projects — precisely the noise that
    makes a divergence list useless.

    So this checks for the markers directly in the directory. A real per-project
    export has POU files at its top level.
    """

    if not directory.is_dir():
        return False
    try:
        if (directory / "PHYSHARDWARE.EXP").is_file():
            return True
        if any(directory.glob("*.GVB")):
            return True
    except OSError:
        return False
    for pattern in ("*.ST", "*.GE", "*.IEC", "*.DIT"):
        try:
            candidates = list(directory.glob(pattern))[:4]
        except OSError:
            continue
        for candidate in candidates:
            try:
                if "@PROPERTIES_EX@" in read_text(candidate)[:400]:
                    return True
            except OSError:
                continue
    return False


def find_nearby_exports(
    project_name: str,
    start: Path,
    max_dirs: int = 80,
    max_results: int = 8,
) -> list[dict]:
    """Look for export artifacts around ``start``, best match for the project first.

    A native project almost never sits alone: the usual layout is

        somewhere/
            PLCOpen XML Export/        TopCutter.xml, RK.xml, ...
            Extended IEC 61131-2 Export/   TopCutter/, RK/, ...
            Motion Works IEC Program/  TopCutter/, RK/, ...

    so if the caller pointed at the native folder, the exports are one or two levels
    up and one level down. Only the parent, the grandparent and their immediate
    children are examined — walking further would be slow and would start suggesting
    unrelated projects on the same disk.

    Name matching matters more than proximity: suggesting ``RK.xml`` for the
    TopCutter project is worse than suggesting nothing.
    """

    from .parsers import plcopen_xml

    start = Path(start).resolve()
    roots: list[Path] = []
    for candidate in (start, start.parent, start.parent.parent):
        if candidate not in roots and candidate.is_dir():
            roots.append(candidate)

    examined: list[Path] = []

    def examine(directory: Path) -> None:
        if len(examined) < max_dirs and directory not in examined:
            examined.append(directory)

    def children_of(directory: Path) -> list[Path]:
        try:
            return sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return []

    for root in roots:
        examine(root)
        for child in children_of(root):
            examine(child)
            # Exports are commonly nested one project level deeper, as
            # `Extended IEC 61131-2 Export/<Project>/`, so the grandchildren are
            # examined too. Without this the Extended export is never found.
            for grandchild in children_of(child):
                examine(grandchild)

    needle = (project_name or "").strip().lower()
    found: list[dict] = []
    seen: set[str] = set()

    for directory in examined:
        try:
            if _is_single_project_export(directory):
                key = str(directory).lower()
                if key not in seen:
                    seen.add(key)
                    found.append({
                        "kind": SourceKind.EXTENDED_IEC.value,
                        "path": str(directory),
                        "nameMatch": bool(needle) and needle in directory.name.lower(),
                    })
        except OSError:
            pass

        try:
            xmls = sorted(directory.glob("*.xml"))
        except OSError:
            xmls = []
        for xml in xmls:
            try:
                if not plcopen_xml.is_plcopen_xml(xml):
                    continue
            except OSError:
                continue
            key = str(xml).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "kind": SourceKind.PLCOPEN_XML.value,
                "path": str(xml),
                "nameMatch": bool(needle) and needle in xml.stem.lower(),
            })

    # An exact/stem name match wins, then any substring match, then the rest.
    def rank(item: dict) -> tuple:
        stem = Path(item["path"]).stem.lower()
        exact = bool(needle) and stem == needle
        return (
            0 if exact else 1,
            0 if item["nameMatch"] else 1,
            0 if item["kind"] == SourceKind.PLCOPEN_XML.value else 1,
            item["path"].lower(),
        )

    found.sort(key=rank)
    return found[:max_results]


def export_guidance(project, path: str | Path | None = None) -> dict | None:
    """What to export, or ``None`` when the loaded sources are sufficient.

    Returns a payload for three situations: nothing exported at all, the PLCopen XML
    export missing, or the Extended export missing.
    """

    sources = set(project.sources or [])
    has_xml = SourceKind.PLCOPEN_XML.value in sources
    has_extended = SourceKind.EXTENDED_IEC.value in sources
    if has_xml and has_extended:
        return None

    name = project.name or ""
    nearby = find_nearby_exports(name, Path(path or project.path or "."))
    matching = [item for item in nearby if item["nameMatch"]]

    if not has_xml and not has_extended:
        payload: dict = {
            "issue": "no_export_found",
            "severity": "blocking",
            "summary": (
                "Only the native MotionWorks project was found. Its code is stored in "
                "a proprietary compound binary (src.st1), so nothing here can supply "
                "source code, variables, types or I/O configuration."
            ),
            "whatYouCannotAnswer": _NATIVE_ONLY_CANNOT,
            "whatIsStillAvailable": _NATIVE_ONLY_CAN,
            "howToExport": {
                "plcopenXml": _PLCOPEN_STEPS,
                "extendedIec": _EXTENDED_STEPS,
            },
            "whyBoth": _WHY_BOTH,
            "nextStep": (
                "Re-run open_project with the export, or put the exports in one "
                "folder and point at that folder to have them merged."
            ),
        }
    elif not has_xml:
        payload = {
            "issue": "plcopen_xml_missing",
            "severity": "degraded",
            "summary": (
                "Only the Extended IEC 61131-2 export was found. It carries no "
                "data-type definitions at all, so function-block interfaces are "
                "unknown, and graphical (LD/FBD) logic is recovered in structure but "
                "only 16% of pin values."
            ),
            "whatYouCannotAnswer": [
                "function-block pin names and types (no type definitions exist here)",
                "complete graphical wiring values",
                "enumerated types and their values",
            ],
            "howToExport": {
                "plcopenXml": _PLCOPEN_STEPS,
            },
            "why": (
                "The PLCopen XML export is authoritative for everything graphical and "
                "is the only source of the data-type library."
            ),
            "nextStep": "Produce the PLCopen XML export and merge the two.",
        }
    else:
        payload = {
            "issue": "extended_iec_missing",
            "severity": "degraded",
            "summary": (
                "Only the PLCopen XML export was found. It deliberately excludes "
                "programming-system-specific elements, so I/O configuration is "
                "missing, task intervals are unlabelled, and AT % addresses are not "
                "available."
            ),
            "whatYouCannotAnswer": [
                "I/O configuration (IOCONFIGURATION.EIO)",
                "task names — the XML carries only priority and interval",
                "AT % hardware addresses",
            ],
            "howToExport": {
                "extendedIec": _EXTENDED_STEPS,
            },
            "why": (
                "Yaskawa's help states the PLCopen XML mechanism 'does not handle "
                "programming system specific elements (i.e. elements which are not "
                "defined by the PLCopen XML specification, such as I/O configuration)'."
            ),
            "nextStep": "Produce the Extended IEC 61131-2 export and merge the two.",
        }

    if nearby:
        # Mark the artifacts already loaded, so the suggestion is never "open the
        # file you just opened".
        loaded_paths = {
            str(Path(value).resolve()).lower()
            for value in (project.source_paths or {}).values()
            if value
        }
        for item in nearby:
            try:
                item["alreadyLoaded"] = str(Path(item["path"]).resolve()).lower() in loaded_paths
            except OSError:
                item["alreadyLoaded"] = False

        payload["foundNearby"] = nearby

    if nearby:
        # Suggest the kinds that are actually missing. Preferring the top-ranked
        # match outright would tell an XML-only caller to open the XML file again.
        wanted = {
            kind
            for kind, present in (
                (SourceKind.PLCOPEN_XML.value, has_xml),
                (SourceKind.EXTENDED_IEC.value, has_extended),
            )
            if not present
        }
        # One path per missing kind, best match first.
        seen_kinds: set[str] = set()
        suggested: list[str] = []
        for item in matching:
            if item["kind"] in wanted and not item.get("alreadyLoaded"):
                if item["kind"] not in seen_kinds:
                    seen_kinds.add(item["kind"])
                    suggested.append(item["path"])

        if suggested:
            payload["suggestedPaths"] = suggested
            payload["suggestedPath"] = suggested[0]
            if len(suggested) > 1:
                payload["nextStep"] = (
                    "Both exports for this project already exist:\n  "
                    + "\n  ".join(suggested)
                    + "\nOne open_project call reads one path, so opening these "
                    "separately gives two partial views rather than a merged one. To "
                    "get the source *and* the types, tasks and I/O together, copy both "
                    "into one folder and point at that folder."
                )
            else:
                payload["nextStep"] = (
                    f"An export that looks like this project already exists: "
                    f"{suggested[0]} — re-run open_project with that path. For the "
                    f"merged view, put both exports for this project in one folder and "
                    f"point at that folder."
                )
        elif matching:
            payload["nextStep"] = (
                "The matching export found nearby is the one already loaded. Produce "
                "the missing export described above and point at both together."
            )
        else:
            payload["nextStep"] = (
                "No export matching this project's name was found, but other exports "
                "exist nearby (listed in foundNearby). Check the name, or export this "
                "project specifically."
            )

    payload["documentation"] = "WORKFLOW.md section 2, 'Export the project'"
    return payload
