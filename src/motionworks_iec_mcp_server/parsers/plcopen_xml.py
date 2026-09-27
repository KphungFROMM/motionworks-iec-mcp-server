"""Parse a PLCopen XML export (IEC 61131-10 / TC6).

This is the most complete of the three sources and the only one that carries
graphical bodies with explicit semantics, so it is treated as authoritative
whenever it is present.

It contributes:

* every POU with its interface (``localVars``/``externalVars``/... per scope);
* **all** languages in one file — ST, LD and FBD;
* the vendor type library (``AXIS_REF``, ``Y_ENGAGE_DATA``, ``MC_*``, ``Y_*``),
  which is what makes generated code checkable against real symbol names;
* global variables **with ``AT %`` addresses and comments**;
* the task tree with priority, interval and program instances;
* ``coordinateInfo`` page geometry.

Two quirks matter:

1. ST bodies are wrapped in XHTML. The text node already contains the real
   newlines, so unwrapping must strip markup **without** re-indenting — tab
   alignment in the source is how the original author aligned ``:=`` and inline
   comments.
2. Task names (``FastTsk``, ``MedTsk``) are **not** in the XML; the elements only
   carry ``priority``/``interval``. The Extended export's ``.EXP`` files carry
   the names, so :mod:`..project` matches them by priority+interval.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from ..model import (
    Body,
    Language,
    Net,
    Node,
    Pou,
    Project,
    Scope,
    SourceKind,
    Task,
    TypeDef,
    Var,
)
from ..util import strip_html
from . import graphical, st as st_parser

SOURCE = SourceKind.PLCOPEN_XML.value

# IEC declaration keywords, mapped to a normalized scope token.
_SCOPE_TAGS = {
    "inputVars": Scope.VAR_INPUT.value,
    "outputVars": Scope.VAR_OUTPUT.value,
    "inOutVars": Scope.VAR_IN_OUT.value,
    "localVars": Scope.VAR.value,
    "externalVars": Scope.VAR_EXTERNAL.value,
    "tempVars": Scope.VAR_TEMP.value,
    "globalVars": Scope.VAR_GLOBAL.value,
}

_TYPE_TAGS = ("LREAL", "REAL", "LINT", "DINT", "INT", "SINT", "ULINT", "UDINT",
              "UINT", "USINT", "BOOL", "BYTE", "WORD", "DWORD", "LWORD", "STRING",
              "WSTRING", "TIME", "DATE", "TOD", "DT", "ANY", "ANY_NUM", "ANY_INT",
              "ANY_REAL", "ANY_BIT", "ANY_STRING")

_BODY_TAGS = ("ST", "LD", "FBD", "SFC", "IL")


def local(tag: str) -> str:
    """Strip an XML namespace from a tag name."""

    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def find(el, tag: str):
    for c in el:
        if local(c.tag) == tag:
            return c
    return None


def findall(el, tag: str) -> list:
    return [c for c in el if local(c.tag) == tag]


def iter_local(el, tag: str):
    for c in el.iter():
        if local(c.tag) == tag:
            yield c


def is_plcopen_xml(path: str | Path) -> bool:
    """Does this file look like a PLCopen XML export?"""

    p = Path(path)
    if not p.is_file() or p.suffix.lower() != ".xml":
        return False
    try:
        with p.open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return b"plcopen.org/xml/tc6" in head or b"<project" in head


def parse(path: str | Path, project: Project) -> Project:
    """Populate ``project`` from a PLCopen XML export."""

    path = Path(path)
    root = ET.parse(str(path)).getroot()

    # One project folder can legitimately hold several PLCopen exports (this
    # workspace holds four). ``sources`` is the set of source *kinds*, so it must
    # not list the same kind once per file; every contributing file is recorded
    # in ``source_paths`` instead.
    if SOURCE not in project.sources:
        project.sources.append(SOURCE)
    existing = project.source_paths.get(SOURCE)
    if existing is None:
        project.source_paths[SOURCE] = str(path)
    elif str(path) not in existing.split("; "):
        project.source_paths[SOURCE] = f"{existing}; {path}"

    header = find(root, "fileHeader")
    if header is not None:
        project.product_name = header.get("productName", "") or project.product_name
        project.product_version = header.get("productVersion", "") or project.product_version

    content = find(root, "contentHeader")
    if content is not None:
        # Remember this file's own project name. ``project.name`` may be
        # overwritten later by another export in the same folder, so POUs are
        # tagged with the name from *their* file rather than whatever ends up on
        # the merged project.
        this_project = content.get("name", "") or project.name
        project.name = this_project or project.name
        project.creation = content.get("creationDateTime", "") or project.creation
    else:
        this_project = project.name

    types_el = find(root, "types")
    if types_el is not None:
        _parse_types(types_el, project)
        # NOTE: in TC6 exports the <pous> element lives *inside* <types>, not at
        # the document root. Reading it from the root silently yields zero POUs,
        # which is the single easiest mistake to make against this format.
        pous_el = find(types_el, "pous")
        if pous_el is not None:
            for pou_el in findall(pous_el, "pou"):
                _parse_pou(pou_el, project, path, this_project)

    # <configurations> is nested inside <instances>. Scan defensively so an
    # export that hoists either element still works.
    for instances_el in _first_or_all(root, "instances"):
        for configs_el in _first_or_all(instances_el, "configurations"):
            for config_el in findall(configs_el, "configuration"):
                _parse_configuration(config_el, project)

    return project


def _first_or_all(el, tag: str) -> list:
    """Return the named child, or the element itself when it already matches."""

    if local(el.tag) == tag:
        return [el]
    child = find(el, tag)
    return [child] if child is not None else []


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------


def _parse_types(types_el, project: Project) -> None:
    """Read every ``<dataType>`` in the export.

    Both the project's own types and the vendor library (``AXIS_REF``,
    ``Y_ENGAGE_DATA``, ``MC_*`` …) live under ``<types>``. In this exporter the
    library sits in ``<dataTypes>`` alongside the project types; ``<pous>`` holds
    the POU definitions. We scan ``dataTypes`` for types and let the POU parser
    handle ``pous``, but a library that is instead expressed as ``<dataType>``
    children of ``<pous>`` is still picked up.
    """

    for container in ("dataTypes", "pous"):
        el = find(types_el, container)
        if el is None:
            continue
        is_library = container == "pous"
        for dt in findall(el, "dataType"):
            _parse_datatype(dt, project, is_library=is_library)


def _parse_datatype(dt, project: Project, is_library: bool = False) -> None:
    name = dt.get("name", "")
    if not name:
        return
    base = find(dt, "baseType")
    td = TypeDef(name=name, source=SOURCE, is_library=is_library)

    struct = find(base, "struct") if base is not None else None
    enum = find(base, "enum") if base is not None else None
    array = find(base, "array") if base is not None else None

    if struct is not None:
        td.kind = "struct"
        td.members = _parse_var_list(struct, Scope.VAR.value, project)
    elif enum is not None:
        td.kind = "enum"
        # The values live under <enum><values><value name="..."/>…, and a value's
        # numeric value is its *ordinal position*, not an attribute. Looking only
        # at direct <value> children of <enum> therefore returns zero values for
        # every enum — which silently removed the whole symbolic vocabulary of
        # the motion library (MC_Direction, MC_BufferMode, Y_EngageMethod, ...).
        values_el = find(enum, "values")
        container = values_el if values_el is not None else enum
        for i, value_el in enumerate(findall(container, "value")):
            # NOTE: the loop variable must not be called `name` — rebinding it
            # makes `project.types.setdefault(name, td)` below register the enum
            # under its *last value's* name, so the enum becomes unfindable.
            value_name = value_el.get("name", "")
            if not value_name:
                continue
            td.enum_values.append({
                "name": value_name,
                "value": value_el.get("value") or str(i),
            })
        enum_base = find(enum, "baseType")
        td.base_type = _type_text(enum_base) if enum_base is not None else ""
    elif array is not None:
        td.kind = "array"
        base_type = find(array, "baseType")
        dim = find(array, "dimension")
        td.base_type = _type_text(base_type) if base_type is not None else ""
        td.comment = dim.get("lower", "") + ".." + dim.get("upper", "") if dim is not None else ""
    else:
        # A derived alias: <baseType><derived name="X"/></baseType>
        derived = None
        if base is not None:
            derived = find(base, "derived")
        td.kind = "alias"
        td.base_type = derived.get("name", "") if derived is not None else _type_text(base)

    project.types.setdefault(name, td)


def _type_text(type_el) -> str:
    """Render a ``<type>`` element back to an IEC type name."""

    if type_el is None:
        return ""
    for child in type_el:
        tag = local(child.tag)
        if tag == "derived":
            return child.get("name", "")
        if tag == "array":
            base = find(child, "baseType")
            dim = find(child, "dimension")
            dims = (
                f"[{dim.get('lower', '')}..{dim.get('upper', '')}]" if dim is not None else ""
            )
            return f"ARRAY {dims} OF {_type_text(base)}"
        return tag
    return ""


def _parse_var_list(container, scope: str, project: Project) -> list[Var]:
    out: list[Var] = []
    for var_el in findall(container, "variable"):
        name = var_el.get("name", "")
        if not name:
            continue
        v = Var(
            name=name,
            type_name=_type_text(find(var_el, "type")),
            scope=scope,
            address=var_el.get("address", "") or "",
            comment=var_el.get("comment", "") or "",
            retain=var_el.get("retain", "false").lower() == "true",
            source=SOURCE,
        )
        init = find(var_el, "initialValue")
        if init is not None:
            val = find(init, "simpleValue")
            if val is not None and val.text:
                v.initial = val.text.strip()
        out.append(v)
    # Structs use the same element name for members.
    return out


# --------------------------------------------------------------------------
# POUs
# --------------------------------------------------------------------------


def _parse_pou(pou_el, project: Project, path: Path, project_name: str = "") -> None:
    name = pou_el.get("name", "")
    if not name:
        return
    pou = Pou(
        name=name,
        pou_type=pou_el.get("pouType", "program"),
        language=Language.UNKNOWN.value,
        file_path=str(path),
        source=SOURCE,
        project_name=project_name or project.name,
    )

    iface = find(pou_el, "interface")
    if iface is not None:
        for tag, scope in _SCOPE_TAGS.items():
            container = find(iface, tag)
            if container is None:
                continue
            parsed = _parse_var_list(container, scope, project)
            if parsed:
                pou.vars.setdefault(scope, []).extend(parsed)
        # Return type for functions.
        ret = find(iface, "returnType")
        if ret is not None:
            pou.description = f"returns {_type_text(ret)}"

    body_el = find(pou_el, "body")
    if body_el is not None:
        pou.body = _parse_body(body_el)

    project.pous[name] = pou
    project.pou_versions.append(pou)
    project.record_origin(SOURCE, pou.project_name, name)


def _parse_body(body_el) -> Body:
    for lang_el in body_el:
        tag = local(lang_el.tag)
        if tag not in _BODY_TAGS:
            continue
        if tag == "ST":
            code = _st_text(lang_el)
            return Body(
                language=Language.ST.value,
                code=code,
                statements=st_parser.count_statements(code),
            )
        if tag in ("LD", "FBD"):
            return graphical.render_xml_body(lang_el, tag)
        return Body(
            language=tag,
            unsupported=f"{tag} bodies are not rendered by this server",
        )
    return Body(language=Language.UNKNOWN.value, unsupported="no body element")


def _st_text(lang_el) -> str:
    """Unwrap an HTML-wrapped ST body into plain IEC source.

    The export nests the code in ``<p xml:space="preserve">`` whose text already
    holds real newlines, so we take the text content and strip residual markup.
    We must not re-indent: tabs align ``:=`` and trailing comments.
    """

    paragraphs = [p for p in lang_el.iter() if local(p.tag) == "p"]
    if paragraphs:
        chunks = []
        for p in paragraphs:
            chunks.append("".join(p.itertext()))
        return strip_html("\n".join(chunks))

    # Some exports carry the ST as a direct text node.
    return strip_html("".join(lang_el.itertext()))


# --------------------------------------------------------------------------
# Configuration / tasks
# --------------------------------------------------------------------------


def _parse_configuration(config_el, project: Project) -> None:
    """Read tasks and globals from a ``<configuration>`` element.

    Both ``<task>`` and ``<globalVars>`` live under the configuration's
    ``<resource>``, not directly under ``<configuration>``. We therefore walk into
    each resource, and only fall back to the configuration's own children if no
    resource is present.
    """

    containers = list(findall(config_el, "resource")) or [config_el]

    for resource in findall(config_el, "resource"):
        if not project.resource_type:
            project.resource_type = resource.get("name", "")

    for container in containers:
        for task_el in findall(container, "task"):
            priority_s = task_el.get("priority")
            interval = task_el.get("interval", "")
            single = task_el.get("single", "")
            programs = [
                pi.get("name", "")
                for pi in findall(task_el, "programInstance")
                if pi.get("name")
            ]
            # This exporter does not write task names. Use a stable synthetic name
            # (containing the priority) and let the merge step rename it from a
            # source that does have names, matched on priority+interval or on the
            # programs it runs. The synthetic name always contains "@" so it is
            # recognisable as provisional.
            name = f"task@{priority_s}" if priority_s is not None else "task@?"
            if interval:
                name = f"task@{priority_s}@{interval}"
            project.tasks[name] = Task(
                name=name,
                task_type="SYSTEM" if single else "CYCLIC",
                interval=interval,
                priority=int(priority_s) if (priority_s or "").isdigit() else None,
                programs=programs,
                source=SOURCE,
            )

        for gv in findall(container, "globalVars"):
            for var_el in findall(gv, "variable"):
                name = var_el.get("name", "")
                if not name:
                    continue
                project.global_vars.append(
                    Var(
                        name=name,
                        type_name=_type_text(find(var_el, "type")),
                        scope=Scope.VAR_GLOBAL.value,
                        address=var_el.get("address", "") or "",
                        comment=var_el.get("comment", "") or "",
                        retain=var_el.get("retain", "false").lower() == "true",
                        source=SOURCE,
                    )
                )
