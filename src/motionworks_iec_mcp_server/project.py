"""Load and merge a MotionWorks IEC project from any of its export forms.

A single path can be a PLCopen XML file, an Extended IEC 61131-2 export
directory, or a native project directory. This module detects which, parses what
it can, and **merges the results while recording every disagreement** instead of
silently letting one source win.

That matters because the exports are projections, not copies. Observed on the
sample set: ``TopCutter.xml`` omits ``TopCutterCutControl``,
``TopCutterCamSetup`` and ``TopCutterFFCamSetup`` (which the Extended export
has), while ``StraightCut`` appears only in the XML. A merged view plus a
divergence list is the only honest answer, and it is what
:func:`~.get_sources` reports.
"""

from __future__ import annotations

import re
from pathlib import Path

from .cache import ParseCache, cached
from .model import Pou, Project, SourceKind, Var
from .parsers import extended_iec, graphical, native, plcopen_xml

_CACHE = ParseCache()

# Fallback language lookup for POUs whose source did not state one.
_EXT_LANGUAGE = {".st": "ST", ".ge": "", ".iec": "", ".dit": ""}


def detect(path: str | Path) -> list[str]:
    """Return the source kinds readable at ``path``.

    A directory may hold more than one export form — a common layout is one
    folder containing both the XML export and the Extended export — so this
    returns every kind found rather than a single guess. Sub-directories one
    level down are inspected too, because exports are frequently nested under a
    project folder.
    """

    p = Path(path)
    found: list[str] = []

    def add(kind: str) -> None:
        if kind not in found:
            found.append(kind)

    if p.is_file():
        if plcopen_xml.is_plcopen_xml(p):
            add(SourceKind.PLCOPEN_XML.value)
        return found

    if not p.is_dir():
        return found

    if extended_iec.is_extended_export(p):
        add(SourceKind.EXTENDED_IEC.value)
    if native.is_native_project(p):
        add(SourceKind.NATIVE.value)

    # Any ``.xml`` directly in the folder is a candidate PLCopen export.
    for candidate in sorted(p.glob("*.xml")):
        if plcopen_xml.is_plcopen_xml(candidate):
            add(SourceKind.PLCOPEN_XML.value)
            break

    for child in sorted(p.iterdir()):
        if not child.is_dir():
            continue
        if extended_iec.is_extended_export(child):
            add(SourceKind.EXTENDED_IEC.value)
        if native.is_native_project(child):
            add(SourceKind.NATIVE.value)
        for candidate in sorted(child.glob("*.xml")):
            if plcopen_xml.is_plcopen_xml(candidate):
                add(SourceKind.PLCOPEN_XML.value)
                break

    return found


def _find_plcopen_xml(root: Path) -> Path | None:
    """Locate the PLCopen XML export in or directly under ``root``."""

    for candidate in sorted(root.glob("*.xml")):
        if plcopen_xml.is_plcopen_xml(candidate):
            return candidate
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        for candidate in sorted(child.glob("*.xml")):
            if plcopen_xml.is_plcopen_xml(candidate):
                return candidate
    return None


def _find_export_dir(root: Path, kind: str) -> Path | None:
    """Locate an Extended or native export in or directly under ``root``."""

    probe = (
        extended_iec.is_extended_export if kind == SourceKind.EXTENDED_IEC.value
        else native.is_native_project
    )
    if probe(root):
        return root
    for child in sorted(root.iterdir()):
        if child.is_dir() and probe(child):
            return child
    return None


_EXPORT_CONTENT_PATTERNS = (
    "*.ST", "*.GE", "*.IEC", "*.DIT", "*.GVB", "*.EIO", "*.EXP", "*.TYP",
    "NODES.LST", "eCLRPouDependencies.dat", "*.mwt",
)


def _export_content_score(root: Path) -> int:
    """How much actual export content sits directly in this folder?

    Used to choose between a folder and a nested folder that both look like
    exports. A bare "does this folder look like an export" test is not enough:
    a parent that merely *contains* an export folder also passes it, and
    preferring the parent means the real export — and all its POUs — is never
    read.
    """

    score = 0
    for pattern in _EXPORT_CONTENT_PATTERNS:
        try:
            score += sum(1 for _ in root.glob(pattern))
        except OSError:
            continue
    return score


def _each_export_dir(root: Path, kind: str) -> list[Path]:
    """The export folder of this kind that ``root`` refers to.

    Considers ``root`` itself and its immediate children, then keeps the *single*
    best candidate by export content. A plain "does this folder look like an
    export" test is not enough, and that was a real bug twice over:

    * a parent that merely *contains* an export folder also passes the test, and
      preferring the parent meant the real export — and all its POUs — was never
      read;
    * an Extended export's ``Configuration/`` subfolder contains ``*.EXP`` files,
      so it also passes the test, and collecting both doubled every variable,
      I/O point and task.

    Both are settled by preferring the candidate with the most export content
    directly inside it.
    """

    probe = (
        extended_iec.is_extended_export if kind == SourceKind.EXTENDED_IEC.value
        else native.is_native_project
    )
    if not root.is_dir():
        return []

    candidates: list[Path] = [root] if probe(root) else []
    for child in sorted(root.iterdir()):
        if child.is_dir() and probe(child):
            candidates.append(child)
    if not candidates:
        return []

    best = max(candidates, key=lambda c: (_export_content_score(c), -len(str(c))))
    return [best]


def _each_plcopen_xml(root: Path) -> list[Path]:
    """Every PLCopen XML export at ``root`` or one level under it, de-duplicated."""

    if not root.is_dir():
        return []
    seen: set[str] = set()
    out: list[Path] = []

    def consider(candidate: Path) -> None:
        if not plcopen_xml.is_plcopen_xml(candidate):
            return
        marker = str(candidate.resolve()).lower()
        if marker in seen:
            return
        seen.add(marker)
        out.append(candidate)

    for candidate in sorted(root.glob("*.xml")):
        consider(candidate)
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        for candidate in sorted(child.glob("*.xml")):
            consider(candidate)
    return out


def _key_for(paths: list[Path]) -> tuple:
    return ParseCache.signature(paths)


def _contributing_files(path: Path, kinds: list[str]) -> list[Path]:
    """Every file whose change should invalidate a cached parse."""

    files: list[Path] = []

    if SourceKind.PLCOPEN_XML.value in kinds:
        files.extend(_each_plcopen_xml(path) if path.is_dir() else [path])

    if SourceKind.EXTENDED_IEC.value in kinds:
        for root in _each_export_dir(path, SourceKind.EXTENDED_IEC.value):
            for pattern in ("*.ST", "*.GE", "*.IEC", "*.DIT", "*.GVB", "*.EIO",
                            "*.EXP", "*.TYP", "*Translation.xml"):
                files.extend(root.rglob(pattern))

    if SourceKind.NATIVE.value in kinds:
        for root in _each_export_dir(path, SourceKind.NATIVE.value):
            for name in ("NODES.LST", "eCLRPouDependencies.dat", "OCIRES.INI",
                         "PROJECT.INF"):
                files.extend(root.rglob(name))
            files.extend(root.rglob("*.SET"))
            files.extend(root.glob("*.mwt"))

    return files


def load(path: str | Path, use_cache: bool = True) -> Project:
    """Parse and merge every readable source at ``path``.

    Raises :class:`SourceNotFoundError` or :class:`SourceUnrecognizedError` so
    the tool layer can return a structured error rather than a traceback.
    """

    p = Path(path)
    if not p.exists():
        raise SourceNotFoundError(str(path))

    kinds = detect(p)
    if not kinds:
        raise SourceUnrecognizedError(str(path))

    key = (str(p.resolve()), tuple(sorted(kinds)), _key_for(_contributing_files(p, kinds)))
    if use_cache:
        hit = _CACHE.get(key)
        if hit is not None:
            return hit

    project = Project(path=str(p))
    if p.is_file():
        project.name = p.stem

    # Order matters: the PLCopen XML is richest, then the Extended export, then
    # the native index (which resolves names for tasks the others leave unnamed).
    for export in _each_plcopen_xml(p) if p.is_dir() else ([p] if p.is_file() else []):
        plcopen_xml.parse(export, project)
    for root in _each_export_dir(p, SourceKind.EXTENDED_IEC.value):
        extended_iec.parse(root, project)
    for root in _each_export_dir(p, SourceKind.NATIVE.value):
        native.parse(root, project)

    # Decode graphical bodies first, then merge per POU name. Order matters: the
    # merge compares body quality, so it must see decoded nets, and the merge is
    # what turns the per-source staging list into the single ``pous`` view.
    _attach_graphical_bodies(project)
    _merge_duplicate_pous(project)
    _infer_languages(project)
    _assign_tasks(project)
    # Recover library interfaces *after* the merge, so block call sites are
    # available for typing pins the .DIT left as a per-file local id. native.parse
    # already ran this once with whatever was parsed at the time; running again here
    # is idempotent and strictly improves the result.
    _recover_library_interfaces(project)
    _reconcile(project)

    if use_cache:
        _CACHE.put(key, project)
    return project


def clear_cache() -> None:
    """Drop every cached parse (used by tests and a manual refresh)."""

    _CACHE.clear()


# --------------------------------------------------------------------------
# Post-processing
# --------------------------------------------------------------------------


def _attach_graphical_bodies(project: Project) -> None:
    """Decode LD/FBD worksheet text into nets.

    Applies to *any* record whose body is raw worksheet text, not just records
    from the Extended export. The Extended parser deliberately stores only the raw
    ``.GE`` text so the decoder stays independently testable; the PLCopen XML
    renderer has already produced nets by this point and is left alone. Restricting
    this to one source was a bug: once POUs from several exports coexisted, the raw
    text from the Extended export could shadow an already-decoded record and the
    graphical logic would silently disappear from the merged view.
    """

    candidates = project.pou_versions or list(project.pous.values())
    for pou in candidates:
        if pou.body.nets:
            continue
        if pou.language not in ("LD", "FBD"):
            continue
        code = pou.body.code or ""
        if "[GRA]" not in code and "[FBS]" not in code:
            continue
        try:
            decoded = graphical.build_body(code, pou.language)
        except Exception as exc:  # a malformed sheet must not sink the project
            pou.body.unsupported = f"failed to decode graphical body: {exc}"
            continue
        pou.body = decoded


def _infer_languages(project: Project) -> None:
    """Fill in a body language where the source did not state one.

    Every parser that can determine the language (the PLCopen XML body tag, the
    ``IEC_LANGUAGE`` property, the ``.ST`` extension) already set it, so this only
    serves records that arrived without one. It must never overwrite a known
    language with a guess.
    """

    for pou in project.pous.values():
        known = (pou.language or "").strip()
        if known and known != "?":
            continue
        suffix = Path(pou.file_path).suffix.lower() if pou.file_path else ""
        guess = _EXT_LANGUAGE.get(suffix, "")
        if not guess and pou.body.language and pou.body.language not in ("", "?"):
            guess = pou.body.language
        if not guess:
            if pou.body.nets:
                guess = "LD"
            elif pou.body.code:
                guess = "ST"
        pou.language = guess or "?"


def _normalise_interval(text: str) -> int | None:
    """Convert a task interval to milliseconds, or ``None`` if unrecognised.

    The PLCopen XML export writes intervals as ``00:00:00.4`` for 4 ms and
    ``00:00:00.20`` for 20 ms — the fractional digits *are* the millisecond count,
    so it cannot be read as a decimal fraction of a second. The Extended export
    writes IEC literals (``T#20ms``). Normalising both is what lets a task from one
    export be recognised in the other.
    """

    if not text:
        return None
    raw = text.strip().upper()
    if raw.startswith("T#"):
        raw = raw[2:]
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(MS|S|US|NS)?", raw)
        if not match:
            return None
        value = float(match.group(1))
        unit = match.group(2) or "MS"
        scale = {"NS": 1e-6, "US": 1e-3, "MS": 1.0, "S": 1000.0}[unit]
        return int(round(value * scale))
    match = re.fullmatch(r"(\d+):(\d+):(\d+)(?:\.(\d+))?", raw)
    if not match:
        return None
    hours, minutes, seconds, frac = match.groups()
    total = (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
    if frac:
        # The digits are the millisecond count, not a decimal fraction.
        total += int(frac)
    return total


def _assign_tasks(project: Project) -> None:
    """Give each program the task it runs under, with a real task name.

    Task names are only stated by the Extended export's ``.EXP`` files and the
    native ``eCLRPouDependencies.dat``. The PLCopen XML export writes only priority
    and interval, so its tasks are named ``task@<priority>@<interval>`` and renamed
    here by matching **priority and normalised interval**.

    A looser rule — matching on any overlap of the two tasks' program lists — was
    tried and removed. When two exports describe different generations of a project
    (which really happens: one sample's XML names ``StraightCut`` and ``CamGen``
    while its Extended export names ``TopCutterCutControl`` instead), a partial
    overlap silently welds unrelated programs onto a real task. Here it attached
    ``ServoTaskSlow`` to ``MedTsk``. Anything that cannot be matched precisely keeps
    its provisional ``@`` name, and a program-list disagreement between matched
    tasks is reported instead of merged.
    """

    unnamed = [t for t in project.tasks.values() if "@" in t.name]
    named = [t for t in project.tasks.values() if "@" not in t.name]

    by_program: dict[str, str] = {}
    for task in named:
        for prog in task.programs:
            by_program.setdefault(prog, task.name)

    renames: dict[str, str] = {}
    for u in unnamed:
        interval_ms = _normalise_interval(u.interval)
        match = None
        for n in named:
            if u.priority is None or n.priority is None:
                continue
            if u.priority != n.priority:
                continue
            if interval_ms is None or _normalise_interval(n.interval) != interval_ms:
                continue
            match = n
            break
        if match is None:
            continue
        renames[u.name] = match.name
        # The named task's program list is kept as-is; a disagreement is reported
        # rather than merged, because merging invents an assignment neither export
        # states.
        if set(u.programs) and set(u.programs) != set(match.programs):
            project.add_divergence(
                "task_program_conflict",
                f"{match.name}: the PLCopen XML export lists "
                f"[{', '.join(u.programs)}] while the export that names tasks lists "
                f"[{', '.join(match.programs)}] — the named export's assignment is "
                f"used; the exports may be different generations of the project",
                [s for s in (SourceKind.PLCOPEN_XML.value,
                             SourceKind.EXTENDED_IEC.value) if s in project.sources],
            )

    # Provisional tasks are attributed *after* renaming, so a program never ends up
    # pointing at a provisional name that no longer exists. A real task name wins
    # (it was inserted first), but a program only the provisional task schedules
    # still gets attributed: its task *name* is unknown, not its schedule, and
    # `CamGen` in a 1 s task beats a blank.
    for u in unnamed:
        final_name = renames.get(u.name, u.name)
        for prog in u.programs:
            by_program.setdefault(prog, final_name)

    # Drop the provisional tasks that were identified, so the task list has one
    # entry per real task rather than a duplicate pair.
    for provisional, real in renames.items():
        kept = project.tasks.get(real)
        dropped = project.tasks.pop(provisional, None)
        if kept is not None and dropped is not None:
            # The named task's program list is *not* extended with the provisional
            # task's programs. Doing so welds programs onto a task that neither
            # export assigns there — it is how `SlowTsk` ended up claiming
            # `EIP_ToCLX`. Disagreements are already reported as divergences above;
            # programs the named export does not place anywhere are attributed to
            # the matched task as a fallback, via `by_program` below.
            if not kept.interval and dropped.interval:
                kept.interval = dropped.interval
            if kept.priority is None:
                kept.priority = dropped.priority

    for pou in project.pous.values():
        if pou.task:
            continue
        task_name = by_program.get(pou.name, "")
        if task_name:
            pou.task = task_name


def _merge_duplicate_pous(project: Project) -> None:
    """Reconcile POUs seen in more than one source.

    The richer record wins, but "richer" is judged per field rather than per
    record, because the exports are incomplete in *different* ways. Concretely,
    ``TopCutter.xml`` ships an empty ``<ST>`` element for ``StraightCut``,
    ``CamGen`` and ``Initialize`` while the Extended export has their real
    source — so an empty body must be treated as a gap to fill, not as a
    legitimate "this POU is empty" answer.

    POUs are grouped by name first. Merging pairwise while iterating a dict that
    holds one entry per name would look correct but never fire, because the
    parsers key ``project.pous`` by name and later sources overwrite earlier
    ones before this function ever runs.
    """

    grouped: dict[str, list[Pou]] = {}
    source_records = project.pou_versions or list(project.pous.values())
    for pou in source_records:
        grouped.setdefault(pou.name, []).append(pou)

    merged: dict[str, Pou] = {}
    for name, versions in grouped.items():
        # Richest record first, so it becomes the carrier for filled-in gaps.
        ordered = sorted(versions, key=_completeness, reverse=True)
        primary = ordered[0]

        # The body is chosen by language-aware rule, independently of which record
        # is the overall "richest" one, because sources are authoritative about
        # different things (see _best_body).
        chosen = _best_body(versions)
        if chosen is not None:
            primary_had_body = _has_body(primary.body)
            primary.body = chosen.body
            # Record where the body came from. The POU's own ``source`` names the
            # record that supplied its metadata/interface, which may be a different
            # export than the one that supplied the code.
            primary.body.source = chosen.source
            if not primary_had_body:
                project.add_divergence(
                    "body_from_other_source",
                    f"{name}: the {primary.source} export shipped this POU with an "
                    f"empty body; the body shown comes from {chosen.source}",
                    sorted({v.source for v in versions if v.source}),
                )

        for other in ordered[1:]:
            if (primary.language not in ("", "?")
                    and other.language not in ("", "?")
                    and other.language != primary.language
                    and _has_body(other.body)):
                project.add_divergence(
                    "pou_conflict",
                    f"{name}: language {primary.language!r} ({primary.source}) vs "
                    f"{other.language!r} ({other.source})",
                    [primary.source, other.source],
                )
            if not primary.description and other.description:
                primary.description = other.description
            if not primary.task and other.task:
                primary.task = other.task
            for scope, vs in other.vars.items():
                if scope not in primary.vars or not primary.vars[scope]:
                    primary.vars[scope] = list(vs)

        if not primary.language or primary.language == "?":
            for other in ordered[1:]:
                if other.language not in ("", "?"):
                    primary.language = other.language
                    break

        merged[name] = primary

    project.pous = merged


def _recover_library_interfaces(project: Project) -> None:
    """Rerun .DIT interface recovery now that POU bodies are merged and decoded.

    Its type table is learned from the project's own declarations, and any pin the
    ``.DIT`` leaves as a per-file local id can be typed from observed call sites —
    neither of which is available until the bodies and nets exist.
    """

    root = project.source_paths.get(SourceKind.NATIVE.value)
    if not root:
        return
    try:
        native.parse_library_interfaces(Path(root), project)
    except Exception as exc:  # recovery is best-effort
        project.add_note(f"library interface recovery failed: {exc}")


def _best_body(versions: list[Pou]) -> Pou | None:
    """Pick which source's body to keep for a POU, returning that record.

    The exports are authoritative about *different things*, so the choice is made
    by language rather than by a blanket "longest wins":

    * **Structured Text** — the Extended export's ``.ST`` file is the plain source
      with original tab alignment; the PLCopen XML wraps ST in XHTML, and
      unwrapping normalises some whitespace. So the plain-text source wins.
    * **LD/FBD** — the PLCopen XML carries explicit connections while the ``.GE``
      worksheet only yields partial pin values, so the XML wins when it has nets.

    Falls back to the version with the most content when neither rule applies.
    """

    if not versions:
        return None

    graphical = [p for p in versions if _has_body(p.body) and p.body.nets]
    if graphical:
        # The PLCopen XML export is authoritative for graphical logic. Net *count*
        # is not a usable signal: the ``.GE`` decoder emits one net per block and
        # per coil, so it routinely reports more nets than the XML's real rungs
        # while resolving far fewer pin values. Choosing by count once silently
        # swapped in the less accurate body.
        prefer = [p for p in graphical if p.source == SourceKind.PLCOPEN_XML.value]
        pool = prefer or graphical
        return max(pool, key=lambda p: (len(p.body.nets), len(p.body.code or "")))

    st_plain = [
        p for p in versions
        if _has_body(p.body) and p.source == SourceKind.EXTENDED_IEC.value
    ]
    if st_plain:
        return max(st_plain, key=lambda p: len(p.body.code or ""))

    with_code = [p for p in versions if _has_body(p.body)]
    if with_code:
        return max(with_code, key=lambda p: len(p.body.code or ""))
    return None


def _completeness(pou: Pou) -> tuple:
    """Rank a POU record from most to least complete."""

    return (
        1 if _has_body(pou.body) else 0,
        1 if pou.body.nets else 0,
        len(pou.body.code or ""),
        sum(len(v) for v in pou.vars.values()),
        1 if pou.language not in ("", "?") else 0,
        1 if pou.description else 0,
    )


def _has_body(body) -> bool:
    """Does this body carry real implementation?"""

    return bool((body.code or "").strip()) or bool(body.nets)


def _reconcile(project: Project) -> None:
    """Report where the sources disagree, without resolving it.

    Comparisons are scoped **per exported project**. A folder can hold several
    unrelated projects (this workspace holds four), and comparing POU sets across
    them produced confident nonsense: every POU of one project was reported as
    "only in the PLCopen XML export" because the other project's Extended export
    naturally did not contain it.
    """

    # POUs the native project declares but no export supplied code for.
    if SourceKind.NATIVE.value in project.sources:
        declared = native.declared_pous(project)
        exported = set(project.pous)
        missing = sorted(set(declared) - exported)
        if missing:
            project.add_divergence(
                "pou_not_exported",
                "declared by the native project but absent from every export "
                f"(code unavailable): {', '.join(missing)}",
                [SourceKind.NATIVE.value],
            )
        extra = sorted(exported - set(declared))
        if extra:
            project.add_divergence(
                "pou_not_in_native",
                f"present in an export but not in the native project tree "
                f"(possible stale export or a different project): {', '.join(extra)}",
                [s for s in project.sources if s != SourceKind.NATIVE.value],
            )

    _reconcile_xml_vs_extended(project)

    # Processor identity: NODES.LST and the XML can disagree after a retarget.
    if project.processor_type:
        seen = set()
        for entry in project.tree:
            if entry.get("record") == "poudep":
                continue
            pt = entry.get("procType")
            if pt:
                seen.add(pt)
        if len(seen) > 1:
            project.add_divergence(
                "proc_type_conflict",
                f"processor type differs across the project tree: {sorted(seen)}",
                [SourceKind.NATIVE.value],
            )


def _reconcile_xml_vs_extended(project: Project) -> None:
    """Compare the XML and Extended exports of the *same* project only."""

    if (SourceKind.PLCOPEN_XML.value not in project.sources
            or SourceKind.EXTENDED_IEC.value not in project.sources):
        return

    xml_pous = project.origin_pous(SourceKind.PLCOPEN_XML.value)
    ext_pous = project.origin_pous(SourceKind.EXTENDED_IEC.value)

    if not xml_pous or not ext_pous:
        return

    # Pair each XML project with the Extended project it most resembles. An
    # exact name match is preferred; failing that, the largest POU-name overlap
    # decides. A project with no overlap at all is left alone, because comparing
    # two unrelated projects produces confident nonsense.
    used: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for xml_name in sorted(xml_pous):
        if xml_name in ext_pous:
            pairs.append((xml_name, xml_name))
            used.add(xml_name)
            continue
        best: tuple[int, str] | None = None
        for ext_name, ext_set in ext_pous.items():
            if ext_name in used:
                continue
            overlap = len(xml_pous[xml_name] & ext_set)
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, ext_name)
        if best is not None:
            pairs.append((xml_name, best[1]))
            used.add(best[1])

    for xml_name, ext_name in pairs:
        only_xml = sorted(xml_pous[xml_name] - ext_pous[ext_name])
        only_ext = sorted(ext_pous[ext_name] - xml_pous[xml_name])
        label = xml_name if xml_name == ext_name else f"{xml_name}/{ext_name}"
        if only_xml:
            project.add_divergence(
                "pou_xml_only",
                f"{label}: only in the PLCopen XML export: {', '.join(only_xml)}",
                [SourceKind.PLCOPEN_XML.value],
            )
        if only_ext:
            project.add_divergence(
                "pou_extended_only",
                f"{label}: only in the Extended IEC export: {', '.join(only_ext)}",
                [SourceKind.EXTENDED_IEC.value],
            )


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class SourceNotFoundError(FileNotFoundError):
    """The requested project path does not exist."""


class SourceUnrecognizedError(ValueError):
    """The path exists but is not a recognised MotionWorks IEC source."""
