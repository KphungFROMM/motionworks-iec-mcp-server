"""Tests for observed-convention analysis.

Conventions are the last gap between "compiles" and "belongs here": an agent that
names a new BOOL ``Start`` in a codebase that uses ``xStart`` produces code a
reviewer will flag. These tests pin the analysis and, importantly, that a weak
signal is reported as weak rather than asserted as a house rule.
"""

from __future__ import annotations

from motionworks_iec_mcp_server import codegen
from motionworks_iec_mcp_server.model import Net, Node, Pou, Project, Task, Var


def _project_with(symbols: list[tuple[str, str]]) -> Project:
    project = Project(name="Conv")
    for name, type_name in symbols:
        project.global_vars.append(Var(name=name, type_name=type_name))
    return project


def _rules(analysis: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for rule in analysis["rules"]:
        out.setdefault(rule["kind"], []).append(rule)
    return out


class TestPrefixRules:
    def test_dominant_prefix_is_reported_with_its_type(self):
        project = _project_with([
            ("xPermit", "BOOL"), ("xReady", "BOOL"), ("xFault", "BOOL"),
            ("xHomed", "BOOL"), ("xEnable", "BOOL"),
            ("iState", "INT"), ("iCount", "INT"), ("iIndex", "INT"), ("iMode", "INT"),
        ])
        analysis = codegen.conventions.analyse(project)
        rules = _rules(analysis)["variablePrefix"]
        by_prefix = {r["rule"].split("'")[1]: r for r in rules}
        assert "boolean" in by_prefix["x"]["rule"]
        assert "integer" in by_prefix["i"]["rule"]
        assert by_prefix["x"]["confidence"] == "strong"

    def test_mixed_prefix_is_not_reported_as_a_rule(self):
        """A prefix used for several types carries no information."""

        project = _project_with([
            ("aOne", "BOOL"), ("aTwo", "INT"), ("aThree", "LREAL"),
            ("aFour", "BOOL"), ("aFive", "INT"),
        ])
        analysis = codegen.conventions.analyse(project)
        assert "variablePrefix" not in _rules(analysis)

    def test_too_little_evidence_is_omitted(self):
        project = _project_with([("xPermit", "BOOL"), ("xReady", "BOOL")])
        analysis = codegen.conventions.analyse(project)
        assert "variablePrefix" not in _rules(analysis)

    def test_naming_summary_maps_family_to_prefix(self):
        project = _project_with([
            ("xPermit", "BOOL"), ("xReady", "BOOL"), ("xFault", "BOOL"),
            ("xHomed", "BOOL"),
            ("rPos", "LREAL"), ("rVel", "LREAL"), ("rAcc", "LREAL"), ("rJerk", "LREAL"),
        ])
        analysis = codegen.conventions.analyse(project)
        assert analysis["namingSummary"]["boolean"] == "x"
        assert analysis["namingSummary"]["real"] == "r"


class TestInstanceRules:
    def test_block_family_prefixed_instances_are_reported(self):
        pou = Pou("P")
        pou.body.nets = [
            Net(number=0, nodes=[
                Node(kind="block", type_name="MC_Power", instance="MC_TopCutter_ServoOn"),
            ]),
            Net(number=1, nodes=[
                Node(kind="block", type_name="MC_Reset", instance="MC_TopCutter_Reset"),
            ]),
            Net(number=2, nodes=[
                Node(kind="block", type_name="MC_Stop", instance="MC_TopCutter_Stop"),
            ]),
        ]
        project = Project()
        project.pous["P"] = pou
        rules = _rules(codegen.conventions.analyse(project)).get("instanceNaming", [])
        assert any("MC_/Y_/FB_" in r["rule"] for r in rules)

    def test_anonymous_instances_are_ignored(self):
        pou = Pou("P")
        pou.body.nets = [Net(number=0, nodes=[
            Node(kind="block", type_name="DIV", instance="@"),
        ])]
        project = Project()
        project.pous["P"] = pou
        assert "instanceNaming" not in _rules(codegen.conventions.analyse(project))


class TestSignalFamilies:
    def test_a_large_family_is_strong_despite_being_a_small_share(self):
        """Confidence follows the member count, not the share of all symbols.

        Dividing by the total rated a family with 89 members as "weak".
        """

        symbols = [(f"EIP_ToCLX_Sig{i:03d}", "BOOL") for i in range(30)]
        symbols += [(f"misc{i}", "INT") for i in range(200)]
        analysis = codegen.conventions.analyse(_project_with(symbols))
        families = _rules(analysis)["signalFamily"]
        target = next(r for r in families if r["rule"].startswith("'EIP_ToCLX_*'"))
        assert target["confidence"] == "strong"
        assert target["evidence"] == 30

    def test_overlapping_families_are_collapsed(self):
        """The longer prefix subsumes the shorter when they cover the same symbols."""

        symbols = [(f"EIP_ToCLX_Axis_{name}", "BOOL")
                   for name in ("Cmd", "Fb", "Alm", "Pos", "Vel", "Torque", "Ready")]
        analysis = codegen.conventions.analyse(_project_with(symbols))
        families = [r["rule"] for r in _rules(analysis)["signalFamily"]]
        assert any("EIP_ToCLX_Axis_*" in f for f in families)
        assert not any(f.startswith("'EIP_ToCLX_*'") for f in families)

    def test_small_families_are_dropped(self):
        analysis = codegen.conventions.analyse(_project_with([
            ("Only_Two", "BOOL"), ("Only_One", "BOOL"),
        ]))
        assert "signalFamily" not in _rules(analysis)


class TestDocumentationHabit:
    def test_commenting_habit_is_reported_when_prevalent(self):
        project = Project()
        for i in range(12):
            project.global_vars.append(
                Var(name=f"xSig{i}", type_name="BOOL", comment="a signal")
            )
        rules = _rules(codegen.conventions.analyse(project)).get("documentation", [])
        assert rules and rules[0]["confidence"] in ("moderate", "strong")

    def test_absent_comments_are_not_reported(self):
        project = _project_with([(f"xSig{i}", "BOOL") for i in range(12)])
        assert "documentation" not in _rules(codegen.conventions.analyse(project))


class TestStructureRules:
    def test_tasks_are_reported_in_priority_order(self):
        project = Project()
        project.tasks["SlowTsk"] = Task(name="SlowTsk", interval="T#100ms",
                                       priority=7, programs=["Slow"])
        project.tasks["FastTsk"] = Task(name="FastTsk", interval="T#4ms",
                                       priority=0, programs=["Fast"])
        analysis = codegen.conventions.analyse(project)
        rule = next(r for r in analysis["rules"] if r["kind"] == "taskStructure")
        assert rule["rule"].index("FastTsk") < rule["rule"].index("SlowTsk")

    def test_language_mix_is_reported(self):
        project = Project()
        project.pous["A"] = Pou("A", language="ST")
        project.pous["B"] = Pou("B", language="LD")
        rule = next(
            r for r in codegen.conventions.analyse(project)["rules"]
            if r["kind"] == "languageMix"
        )
        assert "ST" in rule["rule"] and "LD" in rule["rule"]


class TestEmptyProject:
    def test_no_symbols_yields_no_rules_and_does_not_raise(self):
        analysis = codegen.conventions.analyse(Project())
        assert analysis["ruleCount"] == 0
        assert analysis["symbolCount"] == 0
        assert analysis["guidance"]
