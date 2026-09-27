"""Normalized project model shared by every parser.

The three MotionWorks IEC sources (native project, PLCopen XML export,
Extended IEC 61131-2 export) are each projected onto these dataclasses, so the
MCP tools never need to know which file a fact came from — except when they
need to report divergence, which is why every fact carries its `source`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Iterable


class SourceKind(str, Enum):
    """Which export a fact came from."""

    NATIVE = "native"
    PLCOPEN_XML = "plcopen_xml"
    EXTENDED_IEC = "extended_iec"


class Language(str, Enum):
    ST = "ST"
    LD = "LD"
    FBD = "FBD"
    SFC = "SFC"
    IL = "IL"
    UNKNOWN = "?"


# Languages we render. SFC/IL are parsed defensively but reported as unsupported
# rather than silently mangled (neither appears in any observed export).
RENDERABLE_LANGUAGES = {Language.ST, Language.LD, Language.FBD}
GRAPHICAL_LANGUAGES = {Language.LD, Language.FBD}


class Scope(str, Enum):
    """Where a variable lives / how it is declared."""

    VAR = "VAR"                      # local (program or FB)
    VAR_INPUT = "VAR_INPUT"
    VAR_OUTPUT = "VAR_OUTPUT"
    VAR_IN_OUT = "VAR_IN_OUT"
    VAR_EXTERNAL = "VAR_EXTERNAL"
    VAR_GLOBAL = "VAR_GLOBAL"
    VAR_TEMP = "VAR_TEMP"
    VAR_RETAIN = "VAR_RETAIN"
    VAR_CONFIG = "VAR_CONFIG"
    FUNC_VAR = "FUNC_VAR"


@dataclass
class Var:
    """One variable declaration."""

    name: str
    type_name: str = ""
    scope: str = Scope.VAR.value
    address: str = ""            # e.g. "%MX1.7.0", "%MB1.5000"
    comment: str = ""
    initial: str = ""            # e.g. "FALSE", "0.0"
    retain: bool = False
    dimensions: str = ""         # e.g. "0..9" for arrays
    source: str = ""
    inferred: bool = False       # type was derived, not declared
    members: list[str] = field(default_factory=list)  # nested decls, if any

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type_name, "scope": self.scope}
        if self.address:
            d["address"] = self.address
        if self.comment:
            d["comment"] = self.comment
        if self.initial:
            d["initial"] = self.initial
        if self.dimensions:
            d["dimensions"] = self.dimensions
        if self.retain:
            d["retain"] = True
        if self.inferred:
            d["typeInferred"] = True
        if self.source:
            d["source"] = self.source
        return d


@dataclass
class TypeDef:
    """A user-defined data type: struct, enum, array alias, or subrange."""

    name: str
    kind: str = "struct"          # struct | enum | array | subrange | alias | fb
    members: list[Var] = field(default_factory=list)
    enum_values: list[dict[str, Any]] = field(default_factory=list)
    base_type: str = ""
    comment: str = ""
    source: str = ""
    is_library: bool = False      # came from the vendor firmware library

    def to_dict(self, members: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.base_type:
            d["baseType"] = self.base_type
        if self.comment:
            d["comment"] = self.comment
        if self.is_library:
            d["library"] = True
        if members:
            d["members"] = [m.to_dict() for m in self.members]
            if self.enum_values:
                d["enumValues"] = self.enum_values
        else:
            d["memberCount"] = len(self.members)
        return d


@dataclass
class Node:
    """One element in a rendered network (a block instance, contact, or coil)."""

    kind: str = "block"           # block | contact | coil | operand
    id: int | None = None
    type_name: str = ""           # for blocks: FB name
    instance: str = ""            # for blocks: instance name
    variable: str = ""            # for contacts/coils/operands
    element_type: str = ""        # for contacts/coils: XIC|XIO|OTE|OTL|OTU|...
    params: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0
    unresolved: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.id is not None:
            d["id"] = self.id
        if self.type_name:
            d["type"] = self.type_name
        if self.instance:
            d["instance"] = self.instance
        if self.element_type:
            d["elementType"] = self.element_type
        if self.variable:
            d["variable"] = self.variable
        if self.params:
            d["params"] = self.params
        if self.outputs:
            d["outputs"] = self.outputs
        if self.unresolved:
            d["unresolved"] = True
            if self.note:
                d["note"] = self.note
        return d


@dataclass
class Net:
    """One graphical network (rung / FBD sheet / SFC step region)."""

    number: int = 0
    nodes: list[Node] = field(default_factory=list)
    coils: list[Node] = field(default_factory=list)
    comment: str = ""
    text: str = ""
    label: str = ""

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {"number": self.number}
        if self.label:
            d["label"] = self.label
        if self.comment:
            d["comment"] = self.comment
        if self.nodes:
            d["nodes"] = [n.to_dict() for n in self.nodes]
        if self.coils:
            d["coils"] = [c.to_dict() for c in self.coils]
        if include_text and self.text:
            d["text"] = self.text
        return d


@dataclass
class Body:
    """The implementation of a POU, in whichever language it is written."""

    language: str = Language.UNKNOWN.value
    code: str = ""                 # ST source, or the rendered netlist for LD/FBD
    nets: list[Net] = field(default_factory=list)
    statements: int = 0
    truncated: bool = False
    next_offset: int = 0
    total_lines: int = 0
    unsupported: str = ""          # set for SFC/IL so callers get a clear reason
    source: str = ""               # which export this body actually came from

    def to_dict(self, include_nets: bool = True, include_code: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {"language": self.language}
        if self.source:
            d["source"] = self.source
        if self.unsupported:
            d["unsupported"] = self.unsupported
        if self.statements:
            d["statementCount"] = self.statements
        if self.nets and include_nets:
            d["nets"] = [n.to_dict() for n in self.nets]
            d["networkCount"] = len(self.nets)
        elif self.nets:
            d["networkCount"] = len(self.nets)
        if include_code and self.code:
            d["code"] = self.code
        if self.truncated:
            d["truncated"] = True
            d["nextOffset"] = self.next_offset
            d["totalLines"] = self.total_lines
        return d


@dataclass
class Pou:
    """A program, function block, or function."""

    name: str
    pou_type: str = "program"      # program | function_block | function
    language: str = Language.UNKNOWN.value
    description: str = ""
    vars: dict[str, list[Var]] = field(default_factory=dict)   # scope -> vars
    body: Body = field(default_factory=Body)
    task: str = ""                 # task this program instance runs under
    file_path: str = ""
    source: str = ""
    project_name: str = ""         # which exported project this came from
    pou_id: int | None = None      # native idx from eCLRPouDependencies.dat

    def vars_of(self, *scopes: str) -> list[Var]:
        out: list[Var] = []
        for s in scopes:
            out.extend(self.vars.get(s, []))
        return out

    def all_vars(self) -> list[Var]:
        out: list[Var] = []
        for vs in self.vars.values():
            out.extend(vs)
        return out

    def interface_dict(self) -> dict[str, list[dict[str, Any]]]:
        d: dict[str, list[dict[str, Any]]] = {}
        order = [
            Scope.VAR_INPUT.value,
            Scope.VAR_OUTPUT.value,
            Scope.VAR_IN_OUT.value,
            Scope.VAR.value,
            Scope.VAR_TEMP.value,
            Scope.VAR_EXTERNAL.value,
            Scope.VAR_GLOBAL.value,
        ]
        for scope in order:
            if self.vars.get(scope):
                d[scope] = [v.to_dict() for v in self.vars[scope]]
        # any scope we did not anticipate
        for scope, vs in self.vars.items():
            if scope not in d and vs:
                d[scope] = [v.to_dict() for v in vs]
        return d

    def to_dict(self, include_members: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "pouType": self.pou_type,
            "language": self.language,
        }
        if self.description:
            d["description"] = self.description
        if self.task:
            d["task"] = self.task
        if self.pou_id is not None:
            d["id"] = self.pou_id
        if self.source:
            d["source"] = self.source
        n_vars = sum(len(v) for v in self.vars.values())
        if include_members:
            d["interface"] = self.interface_dict()
        else:
            d["variableCount"] = n_vars
        if self.body.nets:
            d["networkCount"] = len(self.body.nets)
        if self.body.statements:
            d["statementCount"] = self.body.statements
        return d


@dataclass
class Task:
    """A controller task and the programs instantiated in it."""

    name: str
    task_type: str = ""            # CYCLIC | SYSTEM | DEFAULT | ...
    interval: str = ""
    priority: int | None = None
    watchdog: str = ""
    programs: list[str] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.task_type:
            d["taskType"] = self.task_type
        if self.interval:
            d["interval"] = self.interval
        if self.priority is not None:
            d["priority"] = self.priority
        if self.watchdog:
            d["watchdog"] = self.watchdog
        d["programs"] = list(self.programs)
        if self.source:
            d["source"] = self.source
        return d


@dataclass
class IoPoint:
    """One entry from the I/O configuration (.EIO) — a named I/O group."""

    name: str
    direction: str = ""            # INPUT | OUTPUT
    group: str = ""                # e.g. "YEA Input Group <Controller I/O>"
    task: str = ""                 # program instance the I/O is mapped to
    var_addr: str = ""
    end_var_addr: str = ""
    device: str = ""
    driver_name: str = ""
    driver_params: list[str] = field(default_factory=list)
    data_type: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "group": self.group,
            "task": self.task,
            "address": self.var_addr,
            "endAddress": self.end_var_addr,
            "device": self.device,
            "driver": self.driver_name,
            "driverParams": self.driver_params,
            "dataType": self.data_type,
        }


@dataclass
class Divergence:
    """A fact that differs between sources, reported rather than resolved."""

    kind: str                      # pou_missing | tag_conflict | task_conflict | proc_type
    detail: str
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "sources": self.sources}


@dataclass
class Project:
    """The merged view of everything we could read for one machine project."""

    name: str = ""
    path: str = ""
    sources: list[str] = field(default_factory=list)
    source_paths: dict[str, str] = field(default_factory=dict)
    processor_type: str = ""
    resource_type: str = ""
    product_name: str = ""
    product_version: str = ""
    creation: str = ""

    pous: dict[str, Pou] = field(default_factory=dict)
    # Every POU record each source produced, *before* merging. ``pous`` is keyed by
    # name, so it can only hold one version of a POU; without this staging list the
    # last source parsed would silently overwrite the others and merging would
    # have nothing to do.
    pou_versions: list[Pou] = field(default_factory=list)
    types: dict[str, TypeDef] = field(default_factory=dict)
    global_vars: list[Var] = field(default_factory=list)
    tasks: dict[str, Task] = field(default_factory=dict)
    io_points: list[IoPoint] = field(default_factory=list)
    retain_vars: list[Var] = field(default_factory=list)

    tree: list[dict[str, Any]] = field(default_factory=list)      # native NODES.LST
    origins: list[dict[str, Any]] = field(default_factory=list)   # POU provenance
    translations: dict[str, str] = field(default_factory=dict)    # id -> localized text
    library_names: list[str] = field(default_factory=list)        # firmware libraries used
    divergences: list[Divergence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def record_origin(self, source: str, project_name: str, pou_name: str) -> None:
        """Remember that ``source`` contributed ``pou_name``.

        Recorded at parse time rather than derived afterwards, because two
        exports of identically-named projects would otherwise overwrite each
        other in :attr:`pous` and the second contribution would become invisible
        to reconciliation.
        """

        self.origins.append(
            {"source": source, "project": project_name, "pou": pou_name}
        )

    def origin_pous(self, source: str) -> dict[str, set[str]]:
        """``{project_name: {pou_name}}`` for one source, from the origin log."""

        out: dict[str, set[str]] = {}
        for entry in self.origins:
            if entry.get("source") != source:
                continue
            project_name = entry.get("project") or ""
            if not project_name:
                continue
            out.setdefault(project_name, set()).add(entry.get("pou", ""))
        return out

    def add_divergence(self, kind: str, detail: str, sources: Iterable[str] = ()) -> None:
        """Record a divergence once.

        Deduplicated deliberately: reconciliation can legitimately run more than
        once (the library-interface pass reruns after the merge), and repeating the
        same finding would train a reader to ignore the list.
        """

        for existing in self.divergences:
            if existing.kind == kind and existing.detail == detail:
                return
        self.divergences.append(Divergence(kind, detail, list(sources)))

    def add_note(self, text: str) -> None:
        """Record an informational note once (same reasoning as divergences)."""

        if text and text not in self.notes:
            self.notes.append(text)

    def summary(self) -> dict[str, Any]:
        langs: dict[str, int] = {}
        for p in self.pous.values():
            langs[p.language] = langs.get(p.language, 0) + 1
        return {
            "name": self.name,
            "path": self.path,
            "sources": self.sources,
            "sourcePaths": self.source_paths,
            "processorType": self.processor_type,
            "resourceType": self.resource_type,
            "product": self.product_name,
            "productVersion": self.product_version,
            "pouCount": len(self.pous),
            "pous": [p.to_dict() for p in self.pous.values()],
            "typeCount": len(self.types),
            "tagCount": len(self.global_vars),
            "taskCount": len(self.tasks),
            "tasks": [t.to_dict() for t in self.tasks.values()],
            "ioCount": len(self.io_points),
            "languages": langs,
            "divergenceCount": len(self.divergences),
            "divergences": [d.to_dict() for d in self.divergences],
            "notes": self.notes,
        }


def as_jsonable(obj: Any) -> Any:
    """Best-effort conversion of a model object into plain JSON types."""

    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [as_jsonable(o) for o in obj]
    if isinstance(obj, dict):
        return {k: as_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return asdict(obj)
