"""Cross-reference engine: "where is this symbol used?"

MotionWorks projects express the same signal in several places — the global
variable declaration (with its ``%`` address), each POU that reads it, and each
graphical net that wires it to a pin. The index built here flattens all of that
into one searchable surface, which is what makes "where is ``TopCutter_Homed``
used?" a single tool call.

Matches are reported with enough context to be actionable: the POU, the language,
the declaration or net, and the line of code.
"""

from __future__ import annotations

import re

from ..model import Project
from ..util import IEC_KEYWORDS


class XrefIndex:
    """A lazy, token-keyed index over a project's logic and declarations."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self._uses: dict[str, list[dict]] = {}
        self._built = False

    # -- construction ------------------------------------------------------

    def build(self) -> None:
        if self._built:
            return
        self._built = True

        for var in self.project.global_vars:
            if var.name:
                self._record(var.name, {
                    "kind": "declaration",
                    "scope": var.scope,
                    "type": var.type_name,
                    "pou": "",
                    "address": var.address,
                    "comment": var.comment,
                    "source": var.source,
                })

        for typename, td in self.project.types.items():
            self._record(typename, {
                "kind": "type",
                "type": td.kind,
                "pou": "",
                "source": td.source,
            })
            for m in td.members:
                if m.name:
                    self._record(m.name, {
                        "kind": "typeMember",
                        "parent": typename,
                        "type": m.type_name,
                        "pou": "",
                        "source": td.source,
                    })

        for pou in self.project.pous.values():
            for var in pou.all_vars():
                if var.name:
                    self._record(var.name, {
                        "kind": "declaration",
                        "scope": var.scope,
                        "type": var.type_name,
                        "pou": pou.name,
                        "comment": var.comment,
                        "source": var.source,
                    })
            self._index_pou_logic(pou)

    def _index_pou_logic(self, pou) -> None:
        # Structured Text: scan the source line by line, keeping the line text.
        if pou.body.code:
            for lineno, line in enumerate(pou.body.code.splitlines(), start=1):
                for token in _tokens(line):
                    self._record(token, {
                        "kind": "st",
                        "pou": pou.name,
                        "language": pou.language,
                        "line": lineno,
                        "context": line.strip()[:200],
                        "source": pou.source,
                    })

        # Graphical: the rendered nets contain block types, instances and pins.
        for net in pou.body.nets:
            for node in net.nodes:
                if node.type_name:
                    self._record(node.type_name, {
                        "kind": "block",
                        "pou": pou.name,
                        "language": pou.language,
                        "net": net.number,
                        "context": net.text[:200],
                        "source": pou.source,
                    })
                if node.instance and node.instance != "@":
                    self._record(node.instance, {
                        "kind": "instance",
                        "pou": pou.name,
                        "language": pou.language,
                        "net": net.number,
                        "context": net.text[:200],
                        "source": pou.source,
                    })
                for pin, value in node.params.items():
                    for token in _tokens(value):
                        self._record(token, {
                            "kind": "pin",
                            "pou": pou.name,
                            "language": pou.language,
                            "net": net.number,
                            "pin": pin,
                            "context": net.text[:200],
                            "source": pou.source,
                        })
            for coil in net.coils:
                if coil.variable:
                    self._record(coil.variable, {
                        "kind": "coil",
                        "pou": pou.name,
                        "language": pou.language,
                        "net": net.number,
                        "context": net.text[:200],
                        "source": pou.source,
                    })

    def _record(self, key: str, entry: dict) -> None:
        if not key:
            return
        bucket = self._uses.setdefault(key, [])
        # De-duplicate identical facts so a symbol repeated on one line is not
        # reported many times.
        sig = (entry.get("kind"), entry.get("pou"), entry.get("line"),
               entry.get("net"), entry.get("parent"), entry.get("address"))
        for existing in bucket:
            if (existing.get("kind"), existing.get("pou"), existing.get("line"),
                    existing.get("net"), existing.get("parent"),
                    existing.get("address")) == sig:
                return
        bucket.append(entry)

    # -- queries -----------------------------------------------------------

    def uses(self, symbol: str, exact: bool = True) -> list[dict]:
        self.build()
        if exact:
            base = symbol.split(".")[0]
            out = list(self._uses.get(base, []))
            # Also surface qualified references such as Products.Sensor.Bit.
            for key, entries in self._uses.items():
                if key != base and key.split(".")[0] == base:
                    out.extend(entries)
            return out
        return [e for k, v in self._uses.items() if k == symbol for e in v]

    def search(self, pattern: str, limit: int = 400) -> list[dict]:
        """Regex search across declarations, ST lines and rendered nets."""

        self.build()
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc

        hits: list[dict] = []
        # Search the symbol table first: it gives the cleanest answers.
        for symbol in sorted(self._uses):
            if not rx.search(symbol):
                continue
            for entry in self._uses[symbol]:
                hits.append({"symbol": symbol, **entry})
                if len(hits) >= limit:
                    return hits

        # Then raw code, to catch things that are not symbols (comments, values).
        for pou in self.project.pous.values():
            if not pou.body.code:
                continue
            for lineno, line in enumerate(pou.body.code.splitlines(), start=1):
                if rx.search(line):
                    entry = {
                        "symbol": "",
                        "kind": "rawLine",
                        "pou": pou.name,
                        "language": pou.language,
                        "line": lineno,
                        "context": line.strip()[:200],
                        "source": pou.source,
                    }
                    if entry not in hits:
                        hits.append(entry)
                    if len(hits) >= limit:
                        return hits
        return hits

    def symbols(self) -> dict[str, int]:
        """Return ``{symbol: usage count}`` for every indexed symbol."""

        self.build()
        return {k: len(v) for k, v in self._uses.items()}


def _tokens(text: str) -> list[str]:
    """Lexical identifiers in a snippet, excluding IEC keywords and literals."""

    out: list[str] = []
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b", text or ""):
        token = m.group(1)
        if token.upper() in IEC_KEYWORDS:
            continue
        out.append(token)
    return out


def search_xref(project: Project, pattern: str, limit: int = 400) -> list[dict]:
    """Convenience wrapper used by the MCP tool layer."""

    return XrefIndex(project).search(pattern, limit=limit)


def uses_of(project: Project, symbol: str) -> list[dict]:
    return XrefIndex(project).uses(symbol)
