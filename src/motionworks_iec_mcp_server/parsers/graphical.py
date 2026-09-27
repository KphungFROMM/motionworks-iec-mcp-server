"""Decode MotionWorks IEC graphical bodies into a renderable netlist.

MotionWorks emits LD and FBD both as HTML-wrapped PLCopen XML and as plain
text ``.GE`` "worksheet" files. This module handles **both** paths and normalizes
them onto :class:`~..model.Net`, which keeps them independent: the XML path acts
as a correctness oracle for the ``.GE`` decoder.

``.GE`` layout
-------------
A ``.GE`` body is a fixed set of 13 named sections. Every observed file (11
across 4 projects) carries the same set, with these exact field layouts::

    [GRA] <w> <h> 0 0 0 0 0
    [LIT]  <x1> <y1> <x2> <y2> <flags> ""
    [TET]  <x1> <y1> <x2> <y2> <align> <justify> <text…>
    [FBS]  <typeId> <pinId> <x1> <y1> <?> <?> <TypeName> <InstanceName>
    [FPT]  <x1> <y1> <x2> <y2> <Name> 0 0 0 <dir> <flags> 0 @ <TYPE>
    [KOT]  <x1> <y1> <x2> <y2> 0 0 <varId> <Variable>    [VER]  <x> <y> <x> <y> 1 0 1 4294967295 0 <?> 0
    [CON]  <a> <b> <c> <X1> <Y1> <X2> <Y2>
    [AKT] [STT] [TRT] [IBX]  (empty in every observed file)

Two layout details cost real debugging time and are worth stating explicitly:

1. **Sections are written twice.** After the row list the writer emits a second
   bare ``[CON]`` (or ``[FPT]``) header with no rows. Parsers must therefore
   accumulate rows per section name, not reset on a repeated header.
2. **The count line is not always where you expect.** ``[CON]`` in particular
   carries two bare integers (``1`` then ``63``) before its rows. Bare integers
   are skipped wherever they appear, since no section's row format is a single
   integer.

Wiring (``[CON]``) — decoded empirically and cross-checked against the PLCopen
XML export of the same programs:

* ``X1 Y1 X2 Y2`` is a rectangle. When it matches an ``[FPT]`` pin rectangle it
  identifies that pin directly (and therefore its owning block, via geometry);
  when it matches a ``[TET]``/``[LIT]``/``[KOT]`` rectangle it identifies that
  element. Rectangles are used as the primary key because 81 % of rows carry
  them and they are unambiguous.
* ``a`` selects what the *other* end of the wire is: ``0`` -> a canvas point or
  the ordinal in ``c``, ``1`` -> a block pin, ``2`` -> a block.
* ``b``/``c`` are 0-based ordinals into ``[TET] + [LIT]`` (indices 0..N-1 span
  the block instance-name labels as well) or into ``[FBS]``/``[KOT]``.

A ``[TET]`` entry is the *annotation* drawn beside a block pin — the block
instance label and the input/output copies of a variable name. The pin list in
``[FPT]`` is what actually defines the interface. We therefore attach a pin to
its owning block by geometry and take the pin's name from ``[FPT]``, using the
``[TET]`` entry only to label the wire when it is more specific.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..model import Body, Net, Node

SECTION_RE = re.compile(r"^\[(?P<name>[A-Z]+)\]\s*$")


# --------------------------------------------------------------------------
# Data holders
# --------------------------------------------------------------------------


@dataclass
class GePin:
    """A formal parameter pin from ``[FPT]``."""

    x1: int
    y1: int
    x2: int
    y2: int
    name: str = ""
    direction: str = "0"       # "0" = input, "1" = output
    datatype: str = ""
    ordinal: int = 0           # 1-based within its owning block

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass
class GeBlock:
    """A function-block instance from ``[FBS]``.

    Fields 4 and 5 of the ``[FBS]`` row are a **width and height**, and the
    writer often emits ``0 0``. They are kept here as :attr:`width`/:attr:`height`
    and never treated as a corner; :attr:`x2`/:attr:`y2` are filled in from the
    pin extent once pins are attached.
    """

    type_id: str = ""
    pin_id: str = ""
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    type_name: str = ""
    instance: str = ""
    width: int = 0
    height: int = 0
    pins: list[GePin] = field(default_factory=list)
    ordinal: int = 0           # 1-based within [FBS]

    @property
    def inputs(self) -> list[GePin]:
        return [p for p in self.pins if p.direction == "0"]

    @property
    def inouts(self) -> list[GePin]:
        return [p for p in self.pins if p.direction == "2"]

    @property
    def outputs(self) -> list[GePin]:
        # inOut pins are exposed as outputs, because the model has no third
        # category. They must never be reported as inputs.
        return [p for p in self.pins if p.direction not in ("0",)]


@dataclass
class GeElement:
    """A text / literal / contact element."""

    x1: int
    y1: int
    x2: int
    y2: int
    text: str = ""
    kind: str = "text"         # text | literal | contact
    ordinal: int = 0           # 1-based within its section

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass
class GeConnection:
    """One ``[CON]`` row."""

    a: int
    b: int
    c: int
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass
class GeSheet:
    """A fully decoded ``.GE`` worksheet."""

    canvas: tuple[int, ...] = ()
    texts: list[GeElement] = field(default_factory=list)
    literals: list[GeElement] = field(default_factory=list)
    contacts: list[GeElement] = field(default_factory=list)
    blocks: list[GeBlock] = field(default_factory=list)
    connections: list[GeConnection] = field(default_factory=list)
    sections_seen: list[str] = field(default_factory=list)
    malformed_rows: int = 0
    unresolved: int = 0


# --------------------------------------------------------------------------
# Section parsing
# --------------------------------------------------------------------------


def _read_sections(text: str) -> dict[str, list[list[str]]]:
    """Split a ``.GE`` body into ``{SECTION: [row_tokens, ...]}``.

    Repeated section headers accumulate rather than reset, and bare-integer
    lines are dropped wherever they appear (they are count lines, and no
    section's row format is a single integer).
    """

    out: dict[str, list[list[str]]] = {}
    current: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = SECTION_RE.match(line)
        if m:
            current = m.group("name")
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        tokens = line.split()
        if len(tokens) == 1 and tokens[0].lstrip("-").isdigit():
            continue          # count line, or a degenerate row
        out[current].append(tokens)
    return out


def _int(token: str, default: int = 0) -> int:
    try:
        return int(float(token))
    except (TypeError, ValueError):
        return default


def parse_ge(text: str) -> GeSheet:
    """Decode a ``.GE`` body (all sections incl. ``[FPT]``) into a sheet."""

    sections = _read_sections(text)
    sheet = GeSheet(sections_seen=sorted(k for k in sections if not k.startswith("__")))

    gra = sections.get("GRA", [])
    if gra:
        sheet.canvas = tuple(_int(t) for t in gra[0])

    malformed = 0

    for i, row in enumerate(sections.get("TET", [])):
        if len(row) < 7:
            malformed += 1
            continue
        sheet.texts.append(
            GeElement(_int(row[0]), _int(row[1]), _int(row[2]), _int(row[3]),
                      text=" ".join(row[6:]), kind="text", ordinal=i + 1)
        )

    for i, row in enumerate(sections.get("LIT", [])):
        if len(row) < 5:
            malformed += 1
            continue
        sheet.literals.append(
            GeElement(_int(row[0]), _int(row[1]), _int(row[2]), _int(row[3]),
                      text=row[-1].strip('"'), kind="literal", ordinal=i + 1)
        )

    for i, row in enumerate(sections.get("KOT", [])):
        if len(row) < 8:
            malformed += 1
            continue
        sheet.contacts.append(
            GeElement(_int(row[0]), _int(row[1]), _int(row[2]), _int(row[3]),
                      text=" ".join(row[7:]), kind="contact", ordinal=i + 1)
        )

    for i, row in enumerate(sections.get("FBS", [])):
        if len(row) < 8:
            malformed += 1
            continue
        sheet.blocks.append(
            GeBlock(
                type_id=row[0], pin_id=row[1],
                x1=_int(row[2]), y1=_int(row[3]),
                width=_int(row[4]), height=_int(row[5]),
                type_name=row[6], instance=row[7], ordinal=i + 1,
            )
        )

    for row in sections.get("CON", []):
        if len(row) < 6:
            malformed += 1
            continue
        # Rows are `<a> <b> <c> X1 Y1 X2 Y2`, but a handful of exports omit the
        # leading `a`. Anchor on the trailing rectangle, which is always present.
        tail = row[-6:]
        head = row[:-6]
        if len(head) >= 3:
            a, b, c = _int(head[0]), _int(head[1]), _int(head[2])
        elif len(head) == 2:
            a, b, c = _int(head[0]), _int(head[1]), 0
        elif len(head) == 1:
            a, b, c = _int(head[0]), 0, 0
        else:
            a, b, c = 0, 0, 0
        sheet.connections.append(
            GeConnection(a, b, c, _int(tail[0]), _int(tail[1]), _int(tail[2]), _int(tail[3]))
        )

    sheet.malformed_rows = malformed
    _attach_pins(sheet, sections.get("FPT", []))
    return sheet


def _attach_pins(sheet: GeSheet, fpt_rows: list[list[str]]) -> None:
    """Attach each ``[FPT]`` row to its owning block and number it.

    ``[FPT]`` is sheet-global but is written **grouped per block, in the same
    order as ``[FBS]``**. Verified on every observed sheet: ``CamMotion`` has 5
    ``[FBS]`` rows and the 46 ``[FPT]`` rows fall into exactly 5 groups
    (5, 8, 8, 12, 13) matching ``DIV``, ``MC_ReadParameter`` ×2, ``RotaryKnife``,
    ``RotaryKnife_Registration``.

    Grouping is recovered from vertical position, because each block's pins are
    vertically contiguous even though the groups are not globally sorted by y
    (``MC_Reset`` in ``ServoTaskSlow`` sits above ``MC_Power`` yet is declared
    second). We therefore split the pin sequence wherever the vertical gap
    between consecutive pins exceeds a threshold, then bind the groups to the
    blocks in declaration order.

    Note ``[FBS]`` fields 4 and 5 are a **width and height**, not a corner, and
    the writer frequently emits ``0 0`` — treating them as a rectangle makes
    every block zero-sized and detaches every pin. The block rectangle is
    therefore adopted from the pin extent after grouping.
    """

    pins: list[GePin] = []
    for row in fpt_rows:
        if len(row) < 13:
            continue
        pins.append(
            GePin(
                x1=_int(row[0]), y1=_int(row[1]), x2=_int(row[2]), y2=_int(row[3]),
                name=row[4],
                # Index 8 is the pin direction ("0" = input, "1" = output);
                # index 9 is a flag word (4224/4225) and index 12 is the type.
                direction=row[8],
                datatype=row[12],
            )
        )

    if not pins or not sheet.blocks:
        return

    groups = _split_pin_groups(pins, len(sheet.blocks))

    # Bind groups to blocks in declaration order. When the counts disagree the
    # page is not laying out the way we expect, so fall back to nearest-anchor
    # assignment rather than binding a block to the wrong interface.
    if len(groups) == len(sheet.blocks):
        for block, group in zip(sheet.blocks, groups):
            block.pins = group
    else:
        for block, group in zip(sheet.blocks, _fallback_assign(pins, sheet.blocks)):
            block.pins = group

    for block in sheet.blocks:
        block.pins.sort(key=lambda p: (p.y1, p.x1))
        for n, pin in enumerate(block.pins, start=1):
            pin.ordinal = n
        if block.pins:
            # Keep the declared origin (it is the block's anchor, used for
            # clustering and not necessarily the leftmost pin) and extend only
            # the far corner from the pin extent.
            block.x2 = max(p.x2 for p in block.pins)
            block.y2 = max(p.y2 for p in block.pins)


def _split_pin_groups(pins: list[GePin], expected: int) -> list[list[GePin]]:
    """Recover per-block pin groups from the vertical layout.

    A block's pins are vertically contiguous; the gap between two blocks is
    markedly larger than the gap between adjacent pins of one block. We split on
    the largest gaps, using ``expected`` as the target group count, which makes
    the split robust to the exact pixel spacing of a given export.
    """

    if expected <= 1 or len(pins) <= 1:
        return [list(pins)]

    # Split points ranked by the vertical gap they would introduce.
    gaps: list[tuple[int, int]] = []
    for i in range(1, len(pins)):
        gap = pins[i].y1 - pins[i - 1].y2
        gaps.append((gap, i))

    # We need `expected - 1` cuts; take the widest gaps. A gap of zero or less
    # (blocks whose pins interleave vertically) must still be cuttable, so ties
    # are broken by position to stay deterministic.
    ranked = sorted(gaps, key=lambda g: (-g[0], g[1]))
    cuts = sorted(pos for _gap, pos in ranked[: expected - 1])

    groups: list[list[GePin]] = []
    start = 0
    for cut in cuts:
        groups.append(pins[start:cut])
        start = cut
    groups.append(pins[start:])
    return [g for g in groups if g]


def _fallback_assign(pins: list[GePin], blocks: list[GeBlock]) -> list[list[GePin]]:
    """Assign pins to the nearest block by vertical distance.

    Used only when the group count disagrees with the block count, i.e. the
    sheet does not follow the layout we verified. Deterministic and total, so a
    surprising file still yields a usable (if approximate) interface.
    """

    buckets: list[list[GePin]] = [[] for _ in blocks]
    anchors = [(b.y1, b.x1) for b in blocks]
    for pin in pins:
        py = (pin.y1 + pin.y2) / 2.0
        best = min(
            range(len(blocks)),
            key=lambda i: (abs(anchors[i][0] - py), abs(anchors[i][1] - pin.x1)),
        )
        buckets[best].append(pin)
    return buckets


# --------------------------------------------------------------------------
# Netlist construction
# --------------------------------------------------------------------------


def build_body(text: str, language: str) -> Body:
    """Decode a ``.GE`` body into a :class:`Body` with rendered nets."""

    sheet = parse_ge(text)
    nets = render_sheet(sheet)
    return Body(language=language, code=render_text(nets), nets=nets)


def render_sheet(sheet: GeSheet) -> list[Net]:
    """Turn a decoded sheet into nets — one per block instance, plus contacts.

    Pin drivers come from **same-row geometric annotation pairing**: a block's
    input annotation is drawn on the same row as the input pin (to the left) and
    its output annotation on the same row as the output pin (to the right). So
    "the leftmost text whose vertical centre equals this input pin's vertical
    centre" names the driver.

    Measured against the PLCopen XML export of the same six programs
    (``CamGen``, ``CamMotion``, ``EIP_ToCLX``, ``ServoTaskSlow``,
    ``RotaryKnifeCamCreation``, ``RotaryKnifeMotion``) this rule is
    **100 % precise — 8 of 8 recovered values correct, 0 wrong — at 16 % recall**.
    The ordinal route through ``[CON]`` recovers more but produced one wrong
    value in the same sample, so it is deliberately not used for rendering: a
    silently wrong tag name is far worse than a missing one.

    Pins whose annotation cannot be matched are omitted and counted in
    :attr:`GeSheet.unresolved`, and the raw ``[CON]`` table is preserved on the
    sheet so a caller can pursue the remaining wires itself. Use the PLCopen XML
    source when complete graphical fidelity is required.
    """

    by_row_input: dict[int, GeElement] = {}
    for el in sheet.texts:
        cy = (el.y1 + el.y2) // 2
        cur = by_row_input.get(cy)
        if cur is None or el.x1 < cur.x1:
            by_row_input[cy] = el
    by_row_output: dict[int, GeElement] = {}
    for el in sheet.texts:
        cy = (el.y1 + el.y2) // 2
        cur = by_row_output.get(cy)
        if cur is None or el.x1 > cur.x1:
            by_row_output[cy] = el

    nets: list[Net] = []
    ordered = sorted(sheet.blocks, key=lambda b: (b.y1, b.x1))
    unresolved = 0

    for seq, block in enumerate(ordered):
        node = Node(
            kind="block",
            id=block.ordinal,
            type_name=block.type_name,
            instance=block.instance,
            x=float(block.x1),
            y=float(block.y1),
        )
        for pin in block.pins:
            cy = (pin.y1 + pin.y2) // 2
            el = by_row_input.get(cy) if pin.direction == "0" else by_row_output.get(cy)
            value = ""
            if el is not None and el.text:
                value = el.text
            # Never mistake the block's own instance label for a pin driver.
            if value == block.instance:
                value = ""
            if pin.direction == "0":
                if value:
                    node.params[pin.name] = value
                else:
                    unresolved += 1
            elif value:
                node.outputs[pin.name] = value

        net = Net(number=seq, nodes=[node])
        net.text = render_text([net])
        nets.append(net)

    for el in sheet.contacts:
        net = Net(number=len(nets),
                  coils=[Node(kind="coil", variable=el.text, x=float(el.x1), y=float(el.y1))])
        net.text = render_text([net])
        nets.append(net)

    sheet.unresolved = unresolved
    return nets


def render_text(nets: list[Net]) -> str:
    """Render nets as compact, engineer-readable text.

    Mirrors the intent of Studio 5000 NeutralText: one line per block, naming
    each formal parameter and its source, so an agent gets the wiring without the
    geometry and without a context-window blowout. Contacts print as
    ``[XIC Symbol]`` and coils as ``-> Symbol``, so a ladder rung reads the way
    an engineer scans it.
    """

    lines: list[str] = []
    for net in nets:
        for node in net.nodes:
            if node.kind == "contact":
                tag = node.element_type or "XIC"
                lines.append(f"[{tag} {node.variable}]")
                continue
            head = node.type_name or "BLOCK"
            if node.instance and node.instance != "@":
                head = f"{head}({node.instance})"
            parts = [head]
            for pin, value in node.params.items():
                parts.append(f"{pin}={value}")
            if node.outputs:
                outs = ",".join(f"{p}={v}" for p, v in node.outputs.items() if v)
                if outs:
                    parts.append(f"->({outs})")
            lines.append(" ".join(parts))
        for coil in net.coils:
            tag = coil.element_type or ""
            lines.append(f"-> {coil.variable}" + (f" [{tag}]" if tag and tag != "OTE" else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PLCopen XML bodies
# --------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(el, tag: str) -> list:
    return [c for c in el if _local(c.tag) == tag]


def _first_child(el, tag: str):
    for c in el:
        if _local(c.tag) == tag:
            return c
    return None


def render_xml_body(body_el, language: str) -> Body:
    """Render a PLCopen XML ``<LD>``/``<FBD>`` body into nets.

    The XML is the authoritative form: it carries ``refLocalId`` /
    ``formalParameter`` links and explicit ``<contact>``/``<coil>`` elements, so
    no rectangle maths is needed. Rungs are recovered by **connectivity** —
    every element joined by a ``<connection>`` belongs to the same net — which is
    what turns a 23-block ladder into 23 readable rungs instead of one
    unreadable blob.

    Direct element shapes handled:

    ``<block>``          ``typeName`` / ``instanceName`` + per-pin ``<variable>``
    ``<inVariable>``     a source operand
    ``<outVariable>``    a sink operand
    ``<contact>``        normally-open/closed contact, ``<variable>`` + ``refLocalId``
    ``<coil>``           output coil
    ``<leftPowerRail>`` / ``<rightPowerRail>``  power rails, skipped as operands
    """

    # Pass 1: index every element that can be referenced, and read its payload.
    kind_of: dict[str, str] = {}
    name_of: dict[str, str] = {}
    nodes: dict[str, Node] = {}
    edges: list[tuple[str, str]] = []

    def position(el) -> tuple[float, float]:
        pos = _first_child(el, "position")
        if pos is None:
            return 0.0, 0.0
        return float(pos.get("x", 0) or 0), float(pos.get("y", 0) or 0)

    def variable_text(el) -> str:
        var_el = _first_child(el, "variable")
        return (var_el.text or "").strip() if var_el is not None and var_el.text else ""

    def refs(el) -> list[str]:
        out: list[str] = []
        direct = el.get("refLocalId")
        if direct:
            out.append(direct)
        for conn in el.iter():
            if _local(conn.tag) == "connection":
                ref = conn.get("refLocalId")
                if ref:
                    out.append(ref)
        return out

    for el in body_el.iter():
        tag = _local(el.tag)
        local_id = el.get("localId")
        if not local_id:
            continue
        if tag in ("block", "inVariable", "outVariable", "inOutVariable",
                   "contact", "coil", "leftPowerRail", "rightPowerRail"):
            kind_of[local_id] = tag
        if tag in ("inVariable", "inOutVariable", "outVariable", "contact", "coil"):
            name_of[local_id] = variable_text(el)

        if tag == "block":
            x, y = position(el)
            node = Node(
                kind="block",
                id=_int(local_id),
                type_name=el.get("typeName", ""),
                instance=el.get("instanceName", ""),
                x=x, y=y,
            )
            for group_tag in ("inputVariables", "inOutVariables", "outputVariables"):
                group = _first_child(el, group_tag)
                if group is None:
                    continue
                for var in _children(group, "variable"):
                    formal = var.get("formalParameter", "")
                    value = _xml_pin_value(var)
                    if not value:
                        continue
                    if group_tag == "outputVariables":
                        node.outputs[formal] = value
                    else:
                        node.params[formal] = value
            nodes[local_id] = node
        elif tag == "contact":
            x, y = position(el)
            nodes[local_id] = Node(
                kind="contact",
                id=_int(local_id),
                variable=name_of.get(local_id, ""),
                element_type="NO" if el.get("negated", "false") == "false" else "NC",
                x=x, y=y,
            )
        elif tag == "coil":
            x, y = position(el)
            nodes[local_id] = Node(
                kind="coil",
                id=_int(local_id),
                variable=name_of.get(local_id, ""),
                element_type=(el.get("storage") or "OTE").upper() if el.get("storage") else "OTE",
                x=x, y=y,
            )
        elif tag in ("inVariable", "inOutVariable"):
            x, y = position(el)
            nodes[local_id] = Node(
                kind="operand",
                id=_int(local_id),
                variable=name_of.get(local_id, ""),
                x=x, y=y,
            )

    # Pass 2: record every explicit connection as an edge between two elements.
    for el in body_el.iter():
        src = el.get("localId")
        if not src:
            continue
        for conn in el.iter():
            if _local(conn.tag) != "connection":
                continue
            dst = conn.get("refLocalId")
            if dst and src in kind_of and dst in kind_of:
                edges.append((src, dst))

    # Pass 3: union-find the elements into nets, then render each net.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        union(a, b)

    # Resolve "@<localId>" pin references now that every element is indexed.
    # A reference may point at an operand (its name is the value) or at another
    # block's output (the value is "<instance>.<pin>"). Anything still
    # unresolvable keeps the raw id so the gap is visible rather than invented.
    for node in nodes.values():
        if node.kind != "block":
            continue
        for store in (node.params, node.outputs):
            for pin, value in list(store.items()):
                if value.startswith("@"):
                    store[pin] = _resolve_ref(value[1:], name_of, nodes)

    buckets: dict[str, list[Node]] = {}
    for local_id, node in nodes.items():
        if node.kind in ("inVariable", "outVariable", "inOutVariable", "operand"):
            continue
        buckets.setdefault(find(local_id), []).append(node)

    nets: list[Net] = []
    for group in buckets.values():
        blocks = sorted((n for n in group if n.kind == "block"),
                        key=lambda n: (n.y, n.x, n.id or 0))
        contacts = sorted((n for n in group if n.kind == "contact"),
                          key=lambda n: (n.y, n.x, n.id or 0))
        coils = sorted((n for n in group if n.kind == "coil"),
                       key=lambda n: (n.y, n.x, n.id or 0))
        net = Net(number=len(nets), nodes=blocks + contacts, coils=coils)
        net.text = render_text([net])
        nets.append(net)

    nets.sort(key=lambda nt: (
        min((n.y for n in nt.nodes + nt.coils), default=0.0),
        min((n.x for n in nt.nodes + nt.coils), default=0.0),
    ))
    for i, net in enumerate(nets):
        net.number = i

    return Body(language=language, code=render_text(nets), nets=nets)


def _xml_pin_value(var_el) -> str:
    """Encode a pin's connection as a resolvable token.

    The token is ``@<refLocalId>``, suffixed with ``#<formalParameter>`` when the
    connection names a specific output pin of the referenced block — without that
    suffix a block with several outputs could not be told apart. Resolution
    happens in :func:`_resolve_ref` once every element is indexed.
    """

    conn = _first_child(var_el, "connection")
    if conn is None:
        return ""
    ref = conn.get("refLocalId")
    if not ref:
        return ""
    formal = conn.get("formalParameter")
    return f"@{ref}#{formal}" if formal else f"@{ref}"


def _resolve_ref(token: str, name_of: dict[str, str], nodes: dict[str, Node]) -> str:
    """Turn a ``@<ref>[#<pin>]`` token into a readable value.

    Prefers the referenced element's variable name; falls back to naming the
    specific block output it comes from; otherwise returns the raw reference so an
    unresolved wire stays visible instead of being silently blanked.
    """

    ref, _, pin = token.partition("#")
    if pin and ref in nodes:
        target = nodes[ref]
        if target.kind == "block":
            inst = target.instance or target.type_name
            return f"{inst}.{pin}" if inst and inst != "@" else pin
    if ref in name_of and name_of[ref]:
        return name_of[ref]
    target = nodes.get(ref)
    if target is not None and target.kind == "block":
        return target.instance or target.type_name or ref
    return ref
