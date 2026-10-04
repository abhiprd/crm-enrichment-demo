import json
from pathlib import Path

import pytest

from crm import db, gate, learning, rules
from crm.cli import main
from crm.config import Settings
from crm.paths import Paths
from crm.review import create_proposal, get_proposal, handle_decision
from crm.schema import FIELDS
from tests.test_learner import PEOPLE, FakeSol, HS


def settings(mode="demo"):
    return Settings(learner_model="sol", learner_effort="low", rules_mode=mode, extractor_model="luna",
                    prices=(("sol", 2.0, 10.0),))


@pytest.fixture
def setup(tmp_path):
    paths = Paths(tmp_path)
    (tmp_path / "results").mkdir()
    conn = db.connect(tmp_path / "results" / "crm.sqlite")
    conn.execute("INSERT INTO interactions (id, source_type, participants) VALUES ('i1','call',?)", (json.dumps(PEOPLE),))
    ex = conn.execute("INSERT INTO extractions (interaction_id, field, value, status) VALUES ('i1','competitors',?, 'stated')",
                      (json.dumps([{"name": "Clari", "stance": "evaluating"}]),)).lastrowid
    pid = create_proposal(conn, deal_id="d", hubspot_id="1", prop="ai_competitors", current="", proposed="Clari (evaluating)",
                          interaction_id="i1", field="competitors", extraction_id=ex, action="append",
                          evidence={"quotes": [{"quote": "a friend's company uses Clari", "speaker": "Marisol Quinn", "ts": "04:08"}]},
                          context={})
    handle_decision(conn, HS(), pid, "reject", "U1", (), reason="not_crm_worthy", note="passing mention")
    return paths, conn, pid


GOOD = {"rule_text": "Record a competitor only if the buyer is evaluating, using, or has dropped it.", "rationale": "x"}


def fake_gate(passed):
    def _validate(settings, conn, paths, rule_id, client=None):
        net = {f: 0 for f in FIELDS}
        net["competitors"] = 4 if passed else 0
        return {"passed": passed, "net_by_field": net, "reasons": [] if passed else ["competitors net gain 0 is below the minimum 3"],
                "validation_score_before": 0.8, "validation_score_after": 0.82 if passed else 0.8}
    return _validate


def test_demo_mode_rule_is_in_force_before_validation_and_kept_when_the_gate_passes(setup, monkeypatch):
    paths, conn, pid = setup
    said, seen = [], {}

    def validate(*a, **k):
        seen["active_during_gate"] = [r["rule_id"] for r in rules.active_rules(db.connect(paths.root / "results" / "crm.sqlite"))]
        return fake_gate(True)(*a, **k)
    monkeypatch.setattr(gate, "validate_rule", validate)
    out = learning.learn_and_apply(settings(), paths, pid, said.append, client=FakeSol(GOOD))
    assert seen["active_during_gate"] == [out["rule_id"]]  # in force before the gate ran
    assert out["gate"] is True and out["net"] == 4
    assert said[0].startswith("New rule for competitors (unvalidated, ruleset v") and said[1].endswith("validated: net +4 on competitors")
    r = rules.get_rule(conn, out["rule_id"])
    assert (r["status"], r["validated"]) == ("active", 1)


def test_demo_mode_rule_is_retired_automatically_when_the_gate_fails(setup, monkeypatch):
    paths, conn, pid = setup
    monkeypatch.setattr(gate, "validate_rule", fake_gate(False))
    said = []
    out = learning.learn_and_apply(settings(), paths, pid, said.append, client=FakeSol(GOOD))
    assert out["gate"] is False and said[-1].startswith(f"Rule {out['rule_id']} retired automatically")
    assert rules.active_rules(conn) == [] and rules.get_rule(conn, out["rule_id"])["status"] == "retired"


def test_gated_mode_waits_for_the_gate(setup, monkeypatch):
    paths, conn, pid = setup
    seen = {}

    def validate(*a, **k):
        seen["active_during_gate"] = rules.active_rules(db.connect(paths.root / "results" / "crm.sqlite"))
        return fake_gate(True)(*a, **k)
    monkeypatch.setattr(gate, "validate_rule", validate)
    said = []
    out = learning.learn_and_apply(settings("gated"), paths, pid, said.append, client=FakeSol(GOOD))
    assert seen["active_during_gate"] == [] and said[0].startswith("New candidate rule for competitors, not in force")
    assert rules.get_rule(conn, out["rule_id"])["status"] == "active"  # activated only after the pass


def test_no_rule_and_gate_unavailable_are_reported_not_raised(setup, monkeypatch):
    paths, conn, pid = setup
    said = []
    out = learning.learn_and_apply(settings(), paths, pid, said.append, client=FakeSol({"rule_text": None, "rationale": "one-off"}))
    assert out["status"] == "no_rule" and said == ["No new rule from this rejection: one-off"]

    def broken(*a, **k):
        raise SystemExit("results/v0_noise.json is missing")
    monkeypatch.setattr(gate, "validate_rule", broken)
    said2 = []
    out2 = learning.learn_and_apply(settings(), paths, pid, said2.append, client=FakeSol(GOOD))
    assert out2["gate"] == "unavailable" and "Validation could not run" in said2[-1]
    assert rules.get_rule(conn, out2["rule_id"])["status"] == "active"  # demo mode keeps it, unvalidated


def test_cli_rules_list_show_revert(setup, capsys):
    paths, conn, pid = setup
    r = rules.add_candidate(conn, "competitors", "Record a competitor only if evaluated.", "", [pid], "U1")
    v1, _ = rules.activate(conn, r, "U1", "demo", validated=False)
    assert main(["--root", str(paths.root), "rules", "list"]) == 0
    out = capsys.readouterr().out
    assert f"#{r} [active, unvalidated] competitors" in out and f"current ruleset: v{v1}" in out
    assert main(["--root", str(paths.root), "rules", "show", str(r)]) == 0 and "rule_text" in capsys.readouterr().out
    assert main(["--root", str(paths.root), "rules", "revert", "1"]) == 0
    assert "reverted to the rules of v1" in capsys.readouterr().out
    assert rules.active_rules(db.connect(paths.root / "results" / "crm.sqlite")) == []
    assert main(["--root", str(paths.root), "rules", "show", "999"]) == 1
