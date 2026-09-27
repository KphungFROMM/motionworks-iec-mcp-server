"""Code generation helpers: validate agent-written IEC code and emit it.

An LLM writing MotionWorks IEC code gets symbol names wrong — invented tags,
misremembered function-block pins, types that do not exist in the project. The
validator here checks generated code against the *real* project symbol table,
which is the difference between plausible-looking output and something that will
compile.

``render_pou_source`` emits code in the exact shape MotionWorks imports: the
``(*@PROPERTIES_EX@ ... *)`` header, the ``PROGRAM``/``FUNCTION_BLOCK`` line, the
declaration blocks, and the ``(*@KEY@: WORKSHEET ... *)`` body region.
"""

from __future__ import annotations

import re

from ..model import Project, Scope
from ..parsers.st import count_statements, parse_declarations
from ..util import IEC_KEYWORDS, QUALIFIED_IDENT_RE

# MotionWorks' own builtin function-block families. A pin name from these is
# checked against the project's type library, not against IEC keywords.
_STANDARD_FB_PREFIXES = ("MC_", "Y_", "AxisControl", "Jog", "M_Set", "Cam")

# A function-block call: `Instance(args)` or `BlockType(args)`. Deliberately does
# not try to handle nested parentheses inside the argument list; a call written that
# way simply yields no named parameters to check, which is the safe failure.
_FB_CALL_RE = re.compile(r"\b(?P<callee>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>[^()]*)\)")
_NAMED_ARG_RE = re.compile(r"(?P<pin>[A-Za-z_][A-Za-z0-9_]*)\s*:=")

_ST_PROPERTIES = """(*@PROPERTIES_EX@
TYPE: {kind}
LOCALE: 0
IEC_LANGUAGE: {language}
PLC_TYPE: independent
PROC_TYPE: independent
*)
(*@KEY@:DESCRIPTION*)
{description}
(*@KEY@:END_DESCRIPTION*)
{header}

(*Group:Default*)
{declarations}
(*@KEY@: WORKSHEET
NAME: {name}
IEC_LANGUAGE: {language}
*)
{body}
(*@KEY@: END_WORKSHEET *)
{closing}
"""

_CLOSING = {
    "program": "END_PROGRAM",
    "function_block": "END_FUNCTION_BLOCK",
    "function": "END_FUNCTION",
}


class ValidationResult:
    """Outcome of validating a POU against the project."""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.undeclared: list[str] = []
        self.unknown_types: list[str] = []
        self.unknown_blocks: list[str] = []
        # References that differ only in case from the declaration. Legal, but
        # worth flagging for consistency.
        self.case_variants: set[str] = set()

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, kind: str, detail: str, **extra) -> None:
        self.errors.append({"kind": kind, "detail": detail, **extra})

    def warn(self, kind: str, detail: str, **extra) -> None:
        self.warnings.append({"kind": kind, "detail": detail, **extra})

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errorCount": len(self.errors),
            "warningCount": len(self.warnings),
            "errors": self.errors,
            "warnings": self.warnings,
            "undeclaredSymbols": sorted(set(self.undeclared)),
            "unknownTypes": sorted(set(self.unknown_types)),
            "unknownBlocks": sorted(set(self.unknown_blocks)),
            "caseVariants": sorted(self.case_variants),
            "identifiersAreCaseInsensitive": True,
        }


def project_symbols(project: Project) -> set[str]:
    """Every symbol an agent may legitimately reference."""

    out: set[str] = set()
    out.update(v.name for v in project.global_vars if v.name)
    out.update(project.types)
    for pou in project.pous.values():
        out.add(pou.name)
        for var in pou.all_vars():
            if var.name:
                out.add(var.name)
    for task in project.tasks.values():
        out.add(task.name)
    return out


def _known_types(project: Project) -> set[str]:
    base = {
        "BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT",
        "BYTE", "WORD", "DWORD", "LWORD", "REAL", "LREAL", "STRING", "WSTRING",
        "TIME", "DATE", "TOD", "DT", "TIME_OF_DAY", "DATE_AND_TIME",
    }
    return base | set(project.types)


def _known_blocks(
    project: Project,
    reference_pins: dict[str, set[str]] | None = None,
) -> set[str]:
    """Function blocks whose pins we can check.

    Project-defined types plus, crucially, the firmware reference. An Extended-only
    project has **no** type definitions at all (they only exist in the PLCopen XML
    export or in native ``.DIT`` metadata), so without the reference the pin check
    silently did nothing for exactly the projects that need it most.
    """

    out = set(project.types)
    if reference_pins:
        out |= set(reference_pins)
    return out


def _pin_lookup(
    project: Project,
    reference_pins: dict[str, set[str]] | None = None,
) -> dict[str, dict[str, str]]:
    """``{block: {lowercase pin: declared pin}}`` from every source available."""

    out: dict[str, dict[str, str]] = {}
    for name, td in project.types.items():
        if td.members:
            out[name] = {m.name.lower(): m.name for m in td.members}
    if reference_pins:
        for name, pins in reference_pins.items():
            existing = out.setdefault(name, {})
            for pin in pins:
                existing.setdefault(pin.lower(), pin)
    return out


def validate(
    project: Project,
    code: str,
    declared_vars: str = "",
    pou_name: str = "",
    reference_pins: dict[str, set[str]] | None = None,
) -> ValidationResult:
    """Check ``code`` (and optional declarations) against the project.

    Catches the failure modes that matter in practice:

    ``undeclared_tag``      a symbol referenced in code that the project does not
                            declare anywhere (the classic hallucinated tag);
    ``unknown_type``        a declaration using a type the project does not have;
    ``unknown_block``       a call to a function block that does not exist;
    ``unknown_pin``         a formal parameter name that the target FB's interface
                            does not define;
    ``duplicate_declaration`` the same variable declared twice in one scope.

    Identifier comparison is case-insensitive because IEC 61131-3 identifiers are,
    with a style warning on a casing difference. ``reference_pins`` supplies pin
    names for library blocks so that pin checking works even when the project
    carries no type definitions at all.
    """

    result = ValidationResult()
    symbols = project_symbols(project)
    types = _known_types(project)
    blocks = _known_blocks(project, reference_pins)
    pin_lookup_by_block = _pin_lookup(project, reference_pins)

    # --- declared variables -------------------------------------------------
    declarations = parse_declarations(declared_vars, source="generated") if declared_vars else {}
    declared_names: set[str] = set()
    for scope, vars_ in declarations.items():
        seen: set[str] = set()
        for var in vars_:
            if var.name.lower() in seen:
                result.error("duplicate_declaration",
                             f"{var.name!r} declared twice in {scope}", scope=scope)
            seen.add(var.name.lower())
            declared_names.add(var.name)
            base_type = var.type_name.split("[")[0].strip().upper()
            if base_type and base_type not in {t.upper() for t in types} and not _is_derived(base_type):
                result.unknown_types.append(var.type_name)
                result.warn("unknown_type",
                            f"{var.name}: type {var.type_name!r} is not defined in this project",
                            line=var.name)

    # --- instance declarations: `name : FB_Type;` ---------------------------
    # These matter for pin checking, so collect the mapping.
    fb_instances: dict[str, str] = {}
    for scope, vars_ in declarations.items():
        if scope in (Scope.VAR_INPUT.value, Scope.VAR_OUTPUT.value, Scope.VAR_IN_OUT.value):
            continue
        for var in vars_:
            base = var.type_name.split("[")[0].strip()
            if base in blocks:
                fb_instances[var.name] = base
    declared_names_upper = {n.upper() for n in declared_names}

    if not code.strip():
        result.warn("empty_code", "no code supplied to validate")
        return result

    # --- identifiers in code -----------------------------------------------
    #
    # Comparisons are case-insensitive. IEC 61131-3 identifiers are not case
    # sensitive, and this is not theoretical: the RK_DemoOnMP3300iec sample
    # compiles `products` against a declaration of `Products`. A case-sensitive
    # check therefore reports legal code as "undeclared", which is the fastest way
    # to make a validator worthless. A casing difference from the declaration is
    # raised as a style warning instead.
    canonical = _canonical_index(symbols | declared_names | blocks)
    cleaned = re.sub(r"\(\*.*?\*\)", " ", code, flags=re.DOTALL)

    # --- named parameters in function-block calls ---------------------------
    #
    # `fbMove(Execute := xGo, Axis := TopCutter)` is how every MotionWorks developer
    # calls a block. `Execute` and `Axis` are formal parameter names, not symbols, so
    # they must not be reported as undeclared — and because we know the callee's
    # interface, they can be validated as pins instead.
    formal_params: set[str] = set()
    for match in _FB_CALL_RE.finditer(cleaned):
        callee = match.group("callee")
        fb_type = fb_instances.get(callee)
        if fb_type is None:
            resolved = canonical.get(callee.lower())
            if resolved and resolved in pin_lookup_by_block:
                fb_type = resolved
        if fb_type is None:
            continue
        pin_lookup = pin_lookup_by_block.get(fb_type) or {}
        for arg in _NAMED_ARG_RE.finditer(match.group("args")):
            pin = arg.group("pin")
            formal_params.add(pin.lower())
            declared_pin = pin_lookup.get(pin.lower())
            if not pin_lookup:
                continue
            if declared_pin is None:
                result.error(
                    "unknown_pin",
                    f"{callee}({fb_type}).{pin} is not a parameter of {fb_type}; "
                    f"known pins: {', '.join(sorted(pin_lookup.values())[:12])}",
                    instance=callee, fbType=fb_type, pin=pin,
                )
            elif declared_pin != pin:
                result.case_variants.add(f"{pin} → {declared_pin}")

    for m in QUALIFIED_IDENT_RE.finditer(cleaned):
        token = m.group(1)
        head = token.split(".")[0]
        if head.upper() in IEC_KEYWORDS:
            continue
        # A formal parameter in an FB call argument list, already checked above.
        if head.lower() in formal_params:
            continue
        # Function calls and FB instance methods: `Foo(...)` / `Foo.Bar :=`
        declared_as = canonical.get(head.lower())
        if declared_as is not None:
            if declared_as != head:
                result.case_variants.add(f"{head} → {declared_as}")
            continue
        # Numeric/typed literals: INT#20, LREAL#1.0, T#4ms, 16#FF
        if "#" in token:
            continue
        if _is_plausible_member(project, token):
            continue
        result.undeclared.append(head)

    for name in sorted(set(result.undeclared)):
        result.error("undeclared_tag",
                     f"{name!r} is not declared in this project (check spelling or scope)",
                     symbol=name)

    # --- function-block calls and their pins --------------------------------
    for inst, fb_type in fb_instances.items():
        pin_lookup = pin_lookup_by_block.get(fb_type)
        if not pin_lookup:
            continue
        for call in re.finditer(
            rf"\b{re.escape(inst)}\s*\.\s*(?P<pin>[A-Za-z_][A-Za-z0-9_]*)\s*:=",
            cleaned,
            re.IGNORECASE,
        ):
            pin = call.group("pin")
            declared_pin = pin_lookup.get(pin.lower())
            if declared_pin is None:
                result.error(
                    "unknown_pin",
                    f"{inst}({fb_type}).{pin} is not a parameter of {fb_type}; "
                    f"known pins: {', '.join(sorted(pin_lookup.values())[:12])}",
                    instance=inst, fbType=fb_type, pin=pin,
                )
            elif declared_pin != pin:
                result.case_variants.add(f"{pin} → {declared_pin}")

    # Reported last so that casing differences found while checking pins are
    # included; the warning is collected from both passes.
    if result.case_variants:
        examples = sorted(result.case_variants)[:5]
        result.warn(
            "identifier_case",
            "these references differ in case from the declaration "
            f"({', '.join(examples)}). It is accepted, but matching the declared "
            "casing keeps the codebase consistent.",
        )

    return result


def _canonical_index(names: set[str]) -> dict[str, str]:
    """``{lowercase name: declared name}`` for case-insensitive symbol lookup.

    If a project somehow declares two names differing only in case, the first is
    kept and no casing warning is raised for either, because neither is wrong.
    """

    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for name in names:
        key = name.lower()
        if key in out and out[key] != name:
            ambiguous.add(key)
            continue
        out.setdefault(key, name)
    for key in ambiguous:
        out.pop(key, None)
    return out


def _is_derived(base_type: str) -> bool:
    """Types that are legitimately not in the project's own library."""

    return any(base_type.startswith(p) for p in _STANDARD_FB_PREFIXES) or base_type in {
        "AXIS_REF", "TRIGGER_REF", "INPUT_REF", "OUTPUT_REF", "MC_COORD_REF",
    }


def _is_plausible_member(project: Project, token: str) -> bool:
    """Is ``token`` a member access on a known struct instance?

    ``Products.Sensor.Bit`` is legitimate even though ``Products.Sensor`` is not a
    standalone symbol, so a dotted path whose head is declared is accepted.
    Case-insensitive, like every other identifier comparison here.
    """

    if "." not in token:
        return False
    head, _, rest = token.partition(".")
    first = rest.split(".")[0].lower()
    for var in project.global_vars:
        if var.name.lower() != head.lower():
            continue
        td = project.types.get(var.type_name)
        if td is None:
            return True
        return any(m.name.lower() == first for m in td.members) or td.kind != "struct"
    return False


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_pou_source(
    name: str,
    pou_type: str = "program",
    language: str = "ST",
    declaration: str = "",
    code: str = "",
    description: str = "",
) -> str:
    """Emit a POU in the exact shape MotionWorks IEC imports.

    ``header`` is derived from the POU type; ``declaration`` should be the VAR
    blocks (without the trailing ``END_PROGRAM``).
    """

    kind = (pou_type or "program").lower()
    header_kind = "FUNCTION_BLOCK" if kind == "function_block" else kind.upper()
    header = f"{header_kind} {name}"
    closing = _CLOSING.get(kind, "END_PROGRAM")

    decl = (declaration or "").strip()
    if decl and not decl.upper().startswith("VAR"):
        decl = f"VAR\n{decl}\nEND_VAR"
    # Put the declaration on its own lines regardless of how the caller formatted
    # it, since the surrounding template has no newline of its own.
    if decl:
        decl = f"\n\n{decl}\n"

    body = (code or "").rstrip()
    return _ST_PROPERTIES.format(
        kind=("POU" if kind != "data_type" else "DATA_TYPE"),
        language=(language or "ST").upper(),
        description=(description or "").strip(),
        header=header,
        declarations=decl,
        name=name,
        body=("\n" + body + "\n") if body else "\n",
        closing=closing,
    )


def render_type_source(name: str, members: list[dict], comment: str = "") -> str:
    """Emit a ``TYPE ... END_TYPE`` struct definition in MotionWorks shape."""

    lines = ["(*@PROPERTIES_EX@", "TYPE: DATA_TYPE", "LOCALE: 0", "*)", ""]
    if comment:
        lines.extend(["(*@KEY@:DESCRIPTION*)", comment.strip(), "(*@KEY@:END_DESCRIPTION*)"])
    lines.append("")
    lines.append(f"TYPE")
    lines.append(f"\t{name} : STRUCT")
    width = max((len(m.get("name", "")) for m in members), default=0)
    for m in members:
        mn = m.get("name", "")
        mt = m.get("type", "BOOL")
        cmt = m.get("comment", "")
        line = f"\t\t{mn.ljust(width)}\t: {mt};"
        if cmt:
            line += f"\t(*  {cmt}  *)"
        lines.append(line)
    lines.append("\tEND_STRUCT;")
    lines.append("END_TYPE")
    return "\n".join(lines)


def fb_signature(project: Project, fb_name: str) -> dict | None:
    """Return a function block's interface.

    Two sources are tried, because the exports are asymmetric about this:

    1. **A declared type.** Works for project-defined blocks (``RotaryKnife``,
       ``AxisControl``, ``CamGenerator``) and for library blocks that the export
       happens to declare. Only the PLCopen XML export carries types at all.
    2. **Observed usage.** The vendor library's blocks (``MC_Power``, ``Y_CamIn``,
       ``Jog``, ``SetCamMasterCycle``, and everything else supplied by the
       firmware DLLs) are **not** exported as types in any observed project — the
       XML references them by name in ``<block typeName=…>`` and never defines
       them. So the interface is reconstructed from how the project actually
       calls the block: every formal parameter seen, with the values used.

    The result always says which route produced it via ``derivedFrom``, so an
    observed parameter list is never mistaken for an authoritative declaration.
    """

    td = project.types.get(fb_name)
    if td is not None and td.members:
        pins = []
        for m in td.members:
            entry = {
                "name": m.name,
                "type": m.type_name,
                "direction": {
                    "VAR_INPUT": "input",
                    "VAR_OUTPUT": "output",
                    "VAR_IN_OUT": "inOut",
                    "VAR": "local",
                }.get(m.scope, m.scope or "unknown"),
            }
            if m.comment:
                entry["comment"] = m.comment
            pins.append(entry)
        return {
            "name": fb_name,
            "kind": td.kind,
            "library": td.is_library,
            "derivedFrom": (
                "library interface recovered from the project's .DIT metadata and "
                "TYLLIST type table"
                if td.is_library else "declared type"
            ),
            "pinCount": len(pins),
            "pins": pins,
            "source": td.source,
        }

    observed = observed_fb_signature(project, fb_name)
    if observed is not None:
        return observed
    return None


def observed_fb_signature(project: Project, fb_name: str) -> dict | None:
    """Reconstruct a block's interface from how the project instantiates it."""

    counts: dict[str, int] = {}
    sample_values: dict[str, list[str]] = {}
    instances: list[str] = []
    source = ""

    for pou in project.pous.values():
        for net in pou.body.nets:
            for node in net.nodes:
                if node.kind != "block" or node.type_name != fb_name:
                    continue
                source = source or pou.source
                if node.instance and node.instance != "@" and node.instance not in instances:
                    instances.append(node.instance)
                for pin, value in node.params.items():
                    counts[pin] = counts.get(pin, 0) + 1
                    if value and len(sample_values.get(pin, [])) < 3:
                        sample_values.setdefault(pin, []).append(value)
                # Outputs tell us a pin exists even when it is not read.
                for pin in node.outputs:
                    counts.setdefault(pin, 0)

    if not counts:
        return None

    pins = [
        {
            "name": pin,
            "direction": "unknown",
            "usedCount": count,
            "examples": sample_values.get(pin, []),
        }
        for pin, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "name": fb_name,
        "kind": "observed",
        "library": True,
        "derivedFrom": "observed usage in this project (the export does not define "
                       "the vendor library, so parameter names come from real call sites)",
        "pinCount": len(pins),
        "pins": pins,
        "instances": instances[:20],
        "source": source,
    }


def fb_usage_catalog(project: Project) -> dict[str, dict]:
    """Every block type the project uses, with its observed interface.

    This is the practical vocabulary for writing new code: it shows the blocks
    that are actually available and the parameter names they actually take.
    """

    catalog: dict[str, dict] = {}
    for pou in project.pous.values():
        for net in pou.body.nets:
            for node in net.nodes:
                if node.kind != "block" or not node.type_name:
                    continue
                entry = catalog.setdefault(node.type_name, {
                    "name": node.type_name,
                    "useCount": 0,
                    "pins": {},
                    "instances": [],
                    "pous": set(),
                })
                entry["useCount"] += 1
                entry["pous"].add(pou.name)
                if node.instance and node.instance != "@" and len(entry["instances"]) < 10:
                    if node.instance not in entry["instances"]:
                        entry["instances"].append(node.instance)
                for pin in list(node.params) + list(node.outputs):
                    entry["pins"][pin] = entry["pins"].get(pin, 0) + 1

    out: dict[str, dict] = {}
    for name, entry in catalog.items():
        out[name] = {
            "name": name,
            "useCount": entry["useCount"],
            "declaredType": name in project.types,
            "pins": sorted(entry["pins"], key=lambda p: (-entry["pins"][p], p)),
            "instances": entry["instances"],
            "usedIn": sorted(entry["pous"]),
        }
    return out


def st_statement_count(code: str) -> int:
    return count_statements(code)
