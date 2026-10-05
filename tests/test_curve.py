import json

import pytest

from crm import curve, db, evalrun, oracle, propscore, rules
from crm.extractor import Extraction
from crm.ingest import parse_transcript
from crm.paths import Paths
from crm.proposals import Proposal
from crm.schema import FIELDS


def spec(value, status="stated"):
    return {"value": value, "status": status, "evidence": [{"idx": 0, "quote": "x"}]}


NONE = {"value": None, "status": "not_mentioned", "evidence": []}


def pred(field, extraction, before=None):
    return propscore.predicted_proposal(field, extraction, before)


# ---- proposal scoring --------------------------------------------------------------------------

def test_ballpark_budget_house_rule_wants_no_proposal():
    p = pred("budget", spec(50000))
    assert not propscore.proposal_correct("budget", None, p)  # proposing the ballpark is the failure
    assert propscore.proposal_correct("budget", None, pred("budget", NONE))
    assert propscore.proposal_correct("budget", {"action": "set", "value": 50000}, p)


def test_quarter_timeline_needs_the_quarter_not_the_date():
    exp = {"action": "set", "value": "2026-Q4"}
    assert propscore.proposal_correct("decision_timeline", exp, pred("decision_timeline", spec("2026-Q4")))
    assert not propscore.proposal_correct("decision_timeline", exp, pred("decision_timeline", spec("2026-11-30")))
    hedged = {"action": "set", "value": "2026-Q4", "tentative": True}
    assert propscore.proposal_correct("decision_timeline", hedged, pred("decision_timeline", spec("2026-Q4", "hedged")))
    assert not propscore.proposal_correct("decision_timeline", hedged, pred("decision_timeline", spec("2026-Q4")))


def test_competitor_proposal_compares_only_the_new_pairs():
    before = [{"name": "Clari", "stance": "incumbent"}]
    gong = {"name": "Gong", "stance": "evaluating"}
    exp = {"action": "append", "value": [gong]}
    ok = pred("competitors", spec([gong]), before)
    extra = pred("competitors", spec([gong, {"name": "Outreach", "stance": "mentioned"}]), before)
    assert propscore.proposal_correct("competitors", exp, ok)
    assert not propscore.proposal_correct("competitors", exp, extra)
    assert propscore.proposal_correct("competitors", None, pred("competitors", spec([{"name": "Clari", "stance": "incumbent"}]), before))


def test_next_step_and_stage_signal_and_labels():
    step = {"action": "send_proposal", "owner": "Dev Varga", "date": "2026-11-02"}
    assert propscore.proposal_correct("next_step", {"action": "set", "value": step}, pred("next_step", spec([step])))
    assert propscore.proposal_correct("stage_signal", {"action": "note", "value": "advance"}, pred("stage_signal", spec("advance")))
    assert propscore.proposal_correct("pain_points", {"action": "append", "value": ["slow_ramp"]}, pred("pain_points", spec(["slow_ramp"])))
    assert not propscore.proposal_correct("pain_points", {"action": "append", "value": ["slow_ramp"]}, pred("pain_points", spec(["slow_ramp", "x"])))


def test_expected_final_text_merges_into_the_crm_value():
    before = [{"name": "Clari", "stance": "incumbent"}]
    exp = {"action": "append", "value": [{"name": "Gong", "stance": "evaluating"}]}
    assert propscore.expected_final_text("competitors", exp, before) == "Clari (incumbent); Gong (evaluating)"
    assert propscore.expected_final_text("pain_points", {"action": "append", "value": ["b"]}, ["a"]) == "a; b"


# ---- oracle decisions --------------------------------------------------------------------------

def case(cid, field):
    return {"id": cid, "field": field, "note": "n"}


def kf(truth_status, expected, before=None):
    return {"truth": {"value": 1, "status": truth_status}, "expected_proposal": expected, "crm_before": before}


def test_oracle_approves_matches_and_ignores_missing_proposals():
    p = pred("budget", spec(50000))
    assert oracle.decide("budget", kf("stated", {"action": "set", "value": 50000}), p, []).kind == "approve"
    assert oracle.decide("budget", kf("stated", {"action": "set", "value": 50000}), None, []) is None


def test_oracle_rejects_house_rule_exclusions_with_not_crm_worthy():
    d = oracle.decide("budget", kf("stated", None), pred("budget", spec(50000)), [case("ballpark_budget", "budget")])
    assert (d.kind, d.reason) == ("reject", "not_crm_worthy")


def test_oracle_reason_codes_follow_the_trap_and_truth_status():
    assert oracle.reject_reason("champion", "stated", [case("speaker_attribution", "champion")]) == "wrong_speaker"
    assert oracle.reject_reason("decision_timeline", "hedged", []) == "hedged_not_committed"
    assert oracle.reject_reason("budget", "superseded", []) == "stale_or_superseded"
    assert oracle.reject_reason("competitors", "negated", []) == "evidence_doesnt_support"
    assert oracle.reject_reason("budget", "stated", []) == "wrong_value"


def test_oracle_edits_wrong_form_and_rejects_a_passing_mention_that_rides_along():
    d = oracle.decide("decision_timeline", kf("stated", {"action": "set", "value": "2026-Q4"}), pred("decision_timeline", spec("2026-11-30")),
                      [case("quarter_timeline", "decision_timeline")])
    assert (d.kind, d.final) == ("edit", "2026-Q4")
    gong = {"name": "Gong", "stance": "evaluating"}
    both = pred("competitors", spec([gong, {"name": "Outreach", "stance": "mentioned"}]))
    keyed = kf("stated", {"action": "append", "value": [gong]})
    assert oracle.decide("competitors", keyed, both, [case("competitor_threshold", "competitors")]).kind == "reject"
    assert oracle.decide("competitors", keyed, both, []).kind == "edit"  # no house rule: just a wrong value


TRANSCRIPT = """# Call
- Date: 2026-10-01
- Call type: discovery
- Participants:
  - Pat Lee (AE) @ Northbeam [internal]
  - Dev Varga (VP Sales) @ Acme Co
"""


def test_review_extraction_runs_the_slack_handler_path_and_logs(tmp_path):
    conn = db.connect(tmp_path / "c.sqlite")
    block = type("B", (), {"date": "2026-10-01", "participants": [], "utterances": [
        type("U", (), {"idx": 0, "speaker": "Dev", "role": "VP", "text": "we have fifty thousand dollars ballpark", "seconds": 5})()]})()
    fields = {f: dict(NONE) for f in FIELDS}
    fields["budget"] = {"value": 50000, "status": "stated", "evidence": [{"idx": 0, "quote": "fifty thousand dollars"}]}
    fields["stage_signal"] = {"value": "advance", "status": "stated", "evidence": [{"idx": 0, "quote": "fifty thousand dollars"}]}
    ex = Extraction(fields=fields, prompt_version="pv-x")
    key = {"fields": {f: {"truth": {"value": None, "status": "not_mentioned"}, "expected_proposal": None, "crm_before": None} for f in FIELDS}}
    key["fields"]["stage_signal"]["expected_proposal"] = {"action": "note", "value": "advance"}
    key["fields"]["stage_signal"]["truth"] = {"value": "advance", "status": "stated"}
    cases = [case("ballpark_budget", "budget")]
    res = oracle.review_extraction(conn, Paths(tmp_path), "d1", block, ex, key, cases, 1, "m")
    kinds = {d["field"]: (d["kind"], d["reason"], d["outcome"]) for d in res["decisions"]}
    assert kinds["budget"] == ("reject", "not_crm_worthy", "rejected")
    assert kinds["stage_signal"][0] == "approve" and kinds["stage_signal"][2] == "approved"
    row = conn.execute("SELECT review_status, reviewed_by, reject_reason FROM proposals WHERE field='budget'").fetchone()
    assert tuple(row) == ("rejected", "oracle", "not_crm_worthy")


# ---- batching, candidates, grouping, guards ----------------------------------------------------

def test_pick_candidates_one_per_field_most_rejects_first_and_capped():
    rej = [{"field": f, "proposal_id": i} for i, f in enumerate(["budget", "champion", "champion", "competitors", "competitors", "competitors", "use_case"])]
    got = curve.pick_candidates(rej, limit=3)
    assert [r["field"] for r in got] == ["competitors", "champion", "budget"]
    assert curve.pick_candidates([], 3) == []


def test_summarize_reviews_counts_error_types():
    s = curve.summarize_reviews([{"field": "budget", "kind": "approve", "reason": ""}, {"field": "budget", "kind": "reject", "reason": "not_crm_worthy"},
                                 {"field": "decision_timeline", "kind": "edit", "reason": ""}])
    assert (s["reviewed"], s["approved"], s["edited"], s["rejected"]) == (3, 1, 1, 1)
    assert s["reject_reasons"] == {"not_crm_worthy": 1} and s["by_field"]["budget"] == {"approve": 1, "reject": 1}


def test_breakdown_groups_instances_by_case_kind():
    groups = {("a", f): {"group": "other", "cases": []} for f in FIELDS}
    groups[("a", "budget")] = {"group": "house_rule", "cases": ["ballpark_budget"]}
    rec = {"deal_id": "a", "fields": {f: {"score": 1.0, "correct": True} for f in FIELDS}}
    rec["fields"]["budget"] = {"score": 0.0, "correct": False}
    out = curve.breakdown([[rec], [rec]], groups)
    assert out["by_group"]["house_rule"] == {"instances": 1, "mean": 0.0}
    assert out["by_group"]["other"]["instances"] == 8 and out["by_case"]["ballpark_budget"]["mean"] == 0.0


def test_as_metric_swaps_in_proposal_outcomes():
    rec = {"deal_id": "a", "field_score": 1.0, "fields": {f: {"score": 1.0, "correct": True} for f in FIELDS},
           "pfields": {f: {"score": 0.0, "correct": False} for f in FIELDS}}
    assert evalrun.as_metric([[rec]], "fields")[0][0]["field_score"] == 1.0
    swapped = evalrun.as_metric([[rec, {"deal_id": "b", "error": "x"}]], "pfields")[0]
    assert swapped[0]["field_score"] == 0.0 and swapped[1] == {"deal_id": "b", "error": "x"}


def test_test_run_refuses_a_second_run_and_unconfirmed_freeze(tmp_path):
    (tmp_path / "results").mkdir()
    paths = Paths(tmp_path)
    with pytest.raises(SystemExit, match="confirmed frozen"):
        curve.run_test(None, paths, confirmed_frozen=False)
    (tmp_path / "results" / "test_run.json").write_text("{}")
    with pytest.raises(SystemExit, match="already been run"):
        curve.run_test(None, paths, confirmed_frozen=True)
