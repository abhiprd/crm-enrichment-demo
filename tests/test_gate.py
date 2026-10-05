import json
import math

import pytest

from crm import db, gate, rules
from crm.schema import FIELDS


def runs_from(outcomes_by_run):
    """outcomes_by_run: list (per run) of {(deal, field): bool}."""
    out = []
    for o in outcomes_by_run:
        by_deal = {}
        for (d, f), ok in o.items():
            by_deal.setdefault(d, {})[f] = ok
        out.append([{"deal_id": d, "fields": {f: {"correct": by_deal[d].get(f, True), "score": 1.0 if by_deal[d].get(f, True) else 0.0}
                                              for f in FIELDS}, "field_score": 1.0, "unsupported": 0, "extracted": 0,
                     "parse_errors": 0, "in": 1, "out": 1, "reasoning": 0, "cost": 0.0, "ms": 1} for d in by_deal])
    return out


def flat(changes_by_field):
    return {f: changes_by_field.get(f, {"improved": 0, "worsened": 0}) for f in FIELDS}


ALLOW = {f: 1.0 for f in FIELDS}


def test_pass_needs_min_gain_and_to_beat_the_noise_allowance():
    ok = gate.decide(flat({"competitors": {"improved": 5, "worsened": 1}}), "competitors", ALLOW)
    assert ok["passed"] and ok["net_by_field"]["competitors"] == 4
    low = gate.decide(flat({"competitors": {"improved": 3, "worsened": 1}}), "competitors", ALLOW)
    assert not low["passed"] and "below the minimum" in low["reasons"][0]
    noisy = gate.decide(flat({"competitors": {"improved": 4, "worsened": 0}}), "competitors", {**ALLOW, "competitors": 6.0})
    assert not noisy["passed"] and "noise allowance" in noisy["reasons"][0]


def test_other_fields_may_not_lose_more_than_two_net():
    chg = flat({"competitors": {"improved": 6, "worsened": 0}, "champion": {"improved": 0, "worsened": 3}})
    out = gate.decide(chg, "competitors", ALLOW)
    assert not out["passed"] and any("champion lost 3" in r for r in out["reasons"])
    assert gate.decide(flat({"competitors": {"improved": 6, "worsened": 0}, "champion": {"improved": 0, "worsened": 2}}),
                       "competitors", ALLOW)["passed"]


def test_majority_outcome_and_net_changes_over_three_runs():
    base = runs_from([{("a", "competitors"): False}, {("a", "competitors"): False}, {("a", "competitors"): True},
                      ][:3])
    cand = runs_from([{("a", "competitors"): True}, {("a", "competitors"): True}, {("a", "competitors"): False}])
    assert gate.majority_correct(base)[("a", "competitors")] is False  # 1 of 3 correct
    assert gate.majority_correct(cand)[("a", "competitors")] is True  # 2 of 3 correct
    ch = gate.net_changes(base, cand)
    assert ch["competitors"] == {"improved": 1, "worsened": 0} and ch["champion"] == {"improved": 0, "worsened": 0}


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.sqlite")


def test_apply_verdict_retires_a_failing_active_rule_and_activates_a_passing_candidate(conn):
    r1 = rules.add_candidate(conn, "competitors", "rule one", "", [], "U")
    rules.activate(conn, r1, "U", "demo", validated=False)  # demo mode: active but unvalidated
    fail = {"passed": False, "net_by_field": {f: 0 for f in FIELDS}, "reasons": ["competitors net gain 0 is below the minimum 3"],
            "validation_score_before": 0.8, "validation_score_after": 0.8}
    msg = gate.apply_verdict(conn, r1, fail)
    assert msg.startswith("retired automatically") and rules.get_rule(conn, r1)["status"] == "retired"
    assert rules.active_rules(conn) == [] and rules.get_rule(conn, r1)["validated"] == 0
    r2 = rules.add_candidate(conn, "competitors", "rule two", "", [], "U")  # gated mode: still a candidate
    ok = {"passed": True, "net_by_field": {**{f: 0 for f in FIELDS}, "competitors": 4}, "reasons": [],
          "validation_score_before": 0.80, "validation_score_after": 0.82}
    assert gate.apply_verdict(conn, r2, ok) == "validated: net +4 on competitors"
    row = rules.get_rule(conn, r2)
    assert row["status"] == "active" and row["validated"] == 1


def test_allowance_comes_from_the_noise_file(tmp_path):
    from crm.paths import Paths
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "v0_noise.json").write_text(json.dumps({"report": {"flip_rate_by_field": {"competitors": 0.225}}}))
    a = gate.field_allowance(Paths(tmp_path), 40)
    assert math.isclose(a["competitors"], 9.0) and a["budget"] == 0.0
    with pytest.raises(SystemExit):
        gate.field_allowance(Paths(tmp_path / "nowhere"), 40)


# ---- end to end with a fake model --------------------------------------------------------------
import shutil
from pathlib import Path

from crm.config import Settings
from crm.ingest import ingest_inbox
from crm.paths import Paths

REPO = Path(__file__).resolve().parents[1]
FIELD_NAMES = list(FIELDS)


class RuleSensitiveModel:
    """Gets d002's stage_signal wrong unless the prompt contains the learned rule."""

    def __init__(self):
        self.chat = self
        self.completions = self

    def create(self, **kw):
        prompt = kw["messages"][-1]["content"]
        stage = "hold" if "Judge stage by what the call says" in prompt else "advance"
        fields = {f: {"value": None, "status": "not_mentioned", "evidence": []} for f in FIELD_NAMES}
        fields["stage_signal"] = {"value": stage, "status": "stated", "evidence": [{"idx": 19, "quote": "I don't think we'll move faster"}]}

        class U:
            prompt_tokens, completion_tokens, completion_tokens_details = 100, 50, None

        class M:
            content = json.dumps({"fields": fields})

        class Ch:
            message = M()

        class R:
            usage = U
            choices = [Ch]
        return R


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "data").mkdir()
    shutil.copy(REPO / "tests" / "fixtures" / "deals.json", tmp_path / "data" / "deals.json")
    for n in ("taxonomy.json", "taxonomy_definitions.json"):
        shutil.copy(REPO / "data" / n, tmp_path / "data" / n)
    (tmp_path / "prompts").mkdir()
    for n in ("transcript_request.md", "extractor_v0.md"):
        shutil.copy(REPO / "prompts" / n, tmp_path / "prompts" / n)
    p = Paths(tmp_path)
    p.ensure()
    shutil.copy(REPO / "tests" / "fixtures" / "chat_reply_d002.md", p.inbox / "reply.md")
    ingest_inbox(p, settle_seconds=0)
    (tmp_path / "results").mkdir(exist_ok=True)
    (tmp_path / "results" / "v0_noise.json").write_text(json.dumps({"report": {"flip_rate_by_field": {}}}))
    (tmp_path / "results" / "proposal_scores.json").write_text(json.dumps({"v0": {"flip_rate_by_field": {}}}))
    return p


SETTINGS = Settings(extractor_model="luna", extractor_effort="none", prices=(("luna", 0.1, 0.5),))


def test_validate_rule_end_to_end_counts_gain_writes_file_and_applies_verdict(repo, monkeypatch):
    conn = db.connect(repo.root / "results" / "crm.sqlite")
    rid = rules.add_candidate(conn, "stage_signal", "Judge stage by what the call says, not by tone.", "why", [1], "U")
    rules.activate(conn, rid, "U", "demo", validated=False)  # demo mode
    # one validation transcript: a single improved instance is below the fixed minimum of 3
    res = gate.validate_rule(SETTINGS, conn, repo, rid, client=RuleSensitiveModel())
    assert res["changes"]["stage_signal"] == {"improved": 1, "worsened": 0} and res["passed"] is False
    assert (repo.root / "results" / f"gate_{rid}.json").exists() and res["transcripts"] == 1 and res["call_errors"] == 0
    assert res["validation_score_after"] > res["validation_score_before"]
    # with the minimum relaxed for this one-transcript fixture, the same evidence passes
    monkeypatch.setattr(gate, "MIN_NET_GAIN", 1)
    res2 = gate.validate_rule(SETTINGS, conn, repo, rid, client=RuleSensitiveModel())
    assert res2["passed"] is True
    assert gate.apply_verdict(conn, rid, res2) == "validated: net +1 on stage_signal"
    assert rules.get_rule(conn, rid)["validated"] == 1 and rules.get_rule(conn, rid)["status"] == "active"
