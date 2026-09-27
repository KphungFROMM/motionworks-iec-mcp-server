"""Infer the coding conventions a project already follows.

An agent that writes correct IEC but names a new BOOL ``Start`` instead of
``xStart``, or calls a function block ``ReadPos`` instead of
``MC_TopCutter_ReadPos``, produces code that reads as foreign in a review even
though it compiles. Conventions are not written down anywhere; they are visible
only in the existing symbols.

This module derives them from the project's own declarations and instances, and
reports a confidence for each rule so a weak signal is not presented as a rule.
Everything here is *observed*, never imposed — the point is to let generated code
match the codebase it lands in.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..model import Project

# Type families used to describe how prefixes group.
_TYPE_FAMILIES = {
    "BOOL": "boolean",
    "INT": "integer",
    "DINT": "integer",
    "SINT": "integer",
    "UINT": "integer",
    "UDINT": "integer",
    "WORD": "integer",
    "DWORD": "integer",
    "REAL": "real",
    "LREAL": "real",
    "STRING": "string",
    "TIME": "time",
}


@dataclass
class Rule:
    """One observed convention."""

    kind: str
    statement: str
    evidence: int
    confidence: str          # strong | moderate | weak
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "kind": self.kind,
            "rule": self.statement,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }
        if self.examples:
            d["examples"] = self.examples[:8]
        return d


def _confidence(hits: int, total: int) -> str:
    """How much weight to give an observed convention.

    A naming prefix is a deliberate choice, so five consistent uses with *no*
    counterexample is stronger evidence than eight uses with contradictions: the
    ratio matters at least as much as the count.
    """

    if total <= 0:
        return "weak"
    ratio = hits / total
    if ratio >= 1.0 and hits >= 5:
        return "strong"
    if hits >= 8 and ratio >= 0.75:
        return "strong"
    if hits >= 4 and ratio >= 0.5:
        return "moderate"
    return "weak"


def analyse(project: Project, max_examples: int = 6) -> dict:
    """Return observed naming and style conventions for a project."""

    declared: list[tuple[str, str, str]] = []      # (name, type, scope)
    for var in project.global_vars:
        declared.append((var.name, var.type_name, "global"))
    for pou in project.pous.values():
        for var in pou.all_vars():
            declared.append((var.name, var.type_name, pou.name))

    rules: list[Rule] = []
    rules.extend(_prefix_rules(declared, max_examples))
    rules.extend(_instance_rules(project, max_examples))
    rules.extend(_signal_family_rules(declared, max_examples))
    rules.extend(_comment_rules(project))
    rules.extend(_structure_rules(project))

    strong = [r for r in rules if r.confidence == "strong"]
    return {
        "symbolCount": len(declared),
        "ruleCount": len(rules),
        "strongRuleCount": len(strong),
        "rules": [r.to_dict() for r in rules],
        "guidance": (
            "Match these conventions when generating code for this project. They "
            "are observed from the existing symbols, not imposed; rules marked "
            "'weak' had too little evidence to rely on."
        ),
        "namingSummary": _naming_summary(declared),
    }


def _prefix_rules(declared: list[tuple[str, str, str]], max_examples: int) -> list[Rule]:
    """Which leading token maps to which type family.

    A prefix family is only reported when one prefix dominates a single type
    family — that is the signal, and a mixed prefix is reported as mixed.
    """

    by_prefix: dict[str, Counter] = defaultdict(Counter)
    for name, type_name, _scope in declared:
        prefix = _split_prefix(name)
        if not prefix:
            continue
        family = _TYPE_FAMILIES.get(type_name.upper(), "other")
        by_prefix[prefix][family] += 1

    rules: list[Rule] = []
    for prefix, families in sorted(by_prefix.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(families.values())
        if total < 3:
            continue
        family, hits = families.most_common(1)[0]
        if family == "other":
            continue
        confidence = _confidence(hits, total)
        if confidence == "weak":
            continue
        types = sorted({t for n, t, _ in declared if _split_prefix(n) == prefix and
                        _TYPE_FAMILIES.get(t.upper()) == family})
        rules.append(
            Rule(
                kind="variablePrefix",
                statement=(
                    f"'{prefix}' names {family} variables"
                    + (f" ({', '.join(types)})" if types else "")
                ),
                evidence=hits,
                confidence=confidence,
                examples=[n for n, t, _ in declared
                          if _split_prefix(n) == prefix
                          and _TYPE_FAMILIES.get(t.upper()) == family],
            )
        )
    return rules


def _split_prefix(name: str) -> str:
    """The leading lowercase type-hint token of a Hungarian-style name.

    ``udiCutCount`` -> ``udi``; ``xPermit`` -> ``x``; ``TopCutter`` -> ``T``
    (a single capital, which is not treated as a hint).
    """

    out = []
    for ch in name:
        if ch.islower() or ch.isdigit():
            out.append(ch)
        else:
            break
    prefix = "".join(out)
    # A one-character all-caps-ish start or a long word is not a type hint.
    if len(prefix) < 1 or len(prefix) > 4:
        return ""
    if not name[: len(prefix) + 1][-1:].isupper() and len(name) > len(prefix):
        # e.g. "master" — the prefix would run into the word.
        pass
    return prefix if len(name) > len(prefix) else ""


def _instance_rules(project: Project, max_examples: int) -> list[Rule]:
    """How function-block instances are named."""

    instances: list[str] = []
    for pou in project.pous.values():
        for net in pou.body.nets:
            for node in net.nodes:
                if node.kind == "block" and node.instance and node.instance != "@":
                    instances.append(node.instance)

    rules: list[Rule] = []
    if not instances:
        return rules

    counts = Counter(instances)
    fb_prefixed = [i for i in instances if i.startswith(("MC_", "Y_", "FB_"))]
    if fb_prefixed:
        rules.append(Rule(
            kind="instanceNaming",
            statement=(
                "function-block instances are prefixed with the block family "
                "(MC_/Y_/FB_) and describe their purpose, e.g. "
                "'MC_TopCutter_Reset' for an MC_Reset on the TopCutter axis"
            ),
            evidence=len(fb_prefixed),
            confidence=_confidence(len(fb_prefixed), len(instances)),
            examples=fb_prefixed,
        ))
    lowercase = [i for i in instances if i[:1].islower()]
    if lowercase:
        rules.append(Rule(
            kind="instanceNaming",
            statement="some instances use a lower-case 'fb' prefix + purpose (fbCamIn)",
            evidence=len(lowercase),
            confidence=_confidence(len(lowercase), len(instances)),
            examples=lowercase,
        ))
    return rules


def _signal_family_rules(declared: list[tuple[str, str, str]], max_examples: int) -> list[Rule]:
    """Recurring multi-segment signal families, e.g. ``EIP_FromCLX_*``.

    Confidence here is driven by the absolute member count rather than by the share
    of all symbols: a family with 89 members is established regardless of how large
    the project is, and dividing by the total rated it "weak".

    Overlapping families are collapsed — ``EIP_ToCLX_TC_*`` is a subset of
    ``EIP_ToCLX_*``, and reporting both as separate conventions is noise.
    """

    candidates: list[tuple[str, int, list[str]]] = []
    counts: Counter = Counter()
    names_by_prefix: dict[str, list[str]] = defaultdict(list)
    for name, _type, _scope in declared:
        parts = name.split("_")
        for take in (2, 3):
            if len(parts) > take:
                prefix = "_".join(parts[:take])
                counts[prefix] += 1
                names_by_prefix[prefix].append(name)

    for prefix, hits in counts.items():
        if hits >= 4:
            candidates.append((prefix, hits, names_by_prefix[prefix]))

    # Longest (most specific) first, then drop any family whose members are almost
    # all already covered by a more specific one.
    candidates.sort(key=lambda c: (-len(c[0]), -c[1]))
    accepted: list[tuple[str, int, list[str]]] = []
    for prefix, hits, members in candidates:
        covered = False
        for other, _ohits, other_members in accepted:
            if other.startswith(prefix + "_"):
                overlap = len(set(members) & set(other_members))
                if overlap >= 0.8 * len(members):
                    covered = True
                    break
        if not covered:
            accepted.append((prefix, hits, members))

    accepted.sort(key=lambda c: -c[1])
    rules: list[Rule] = []
    for prefix, hits, members in accepted[:6]:
        if hits >= 20:
            confidence = "strong"
        elif hits >= 8:
            confidence = "moderate"
        else:
            confidence = "weak"
        rules.append(Rule(
            kind="signalFamily",
            statement=f"'{prefix}_*' is an established signal family ({hits} variables)",
            evidence=hits,
            confidence=confidence,
            examples=members,
        ))
    return rules


def _comment_rules(project: Project) -> list[Rule]:
    """Whether declarations carry explanatory comments."""

    total = commented = 0
    for var in project.global_vars:
        total += 1
        if var.comment:
            commented += 1
    for pou in project.pous.values():
        for var in pou.all_vars():
            total += 1
            if var.comment:
                commented += 1
    if total < 10:
        return []
    if commented / total < 0.3:
        return []
    return [Rule(
        kind="documentation",
        statement=(
            "declarations carry a trailing comment explaining the signal; do the "
            "same for new variables"
        ),
        evidence=commented,
        confidence=_confidence(commented, total),
        examples=[v.name for v in project.global_vars if v.comment][:6],
    )]


def _structure_rules(project: Project) -> list[Rule]:
    """Task structure and language mix, which shape where new code belongs."""

    rules: list[Rule] = []
    if project.tasks:
        named = [t for t in project.tasks.values() if t.programs]
        if named:
            order = sorted(
                named,
                key=lambda t: (t.priority if t.priority is not None else 99, t.name),
            )
            rules.append(Rule(
                kind="taskStructure",
                statement=(
                    "tasks run in priority order "
                    + ", ".join(f"{t.name}({t.interval or t.task_type})" for t in order[:6])
                    + "; put time-critical logic in the fastest task that suffices"
                ),
                evidence=len(order),
                confidence="strong",
                examples=[p for t in order for p in t.programs][:8],
            ))
    langs = Counter(p.language for p in project.pous.values() if p.language)
    if langs:
        rules.append(Rule(
            kind="languageMix",
            statement=(
                "body languages in use: "
                + ", ".join(f"{k}×{v}" for k, v in langs.most_common())
                + "; match the language of the surrounding POUs"
            ),
            evidence=sum(langs.values()),
            confidence="strong",
        ))
    return rules


def _naming_summary(declared: list[tuple[str, str, str]]) -> dict[str, str]:
    """``{type family: most common prefix}`` — a quick lookup for new variables."""

    by_family: dict[str, Counter] = defaultdict(Counter)
    for name, type_name, _scope in declared:
        prefix = _split_prefix(name)
        if not prefix:
            continue
        family = _TYPE_FAMILIES.get(type_name.upper())
        if family:
            by_family[family][prefix] += 1
    return {
        family: counter.most_common(1)[0][0]
        for family, counter in sorted(by_family.items())
        if counter
    }
