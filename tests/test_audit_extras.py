"""More audit tests (docs/TEST_AUDIT.md): the proposal builder's fields, the hand-review report, spend statistics,
learn-split error surfacing with a positive case, and which field a gate result is read from."""

import json
import sqlite3

import pytest

from crm import charts, db, evalcmds, extractor, gate, handreview, learning, rules
from crm.paths import Paths
from crm.proposals import build_proposals, merge_competitors, parse_competitors
from crm.review import create_proposal, handle_decision
from crm.schema import FIELDS
from tests.test_evalcmds import paths as eval_paths  # noqa: F401  (fixture)
from tests.test_learning import FakeSol, GOOD, fake_gate, setup, settings  # noqa: F401  (setup is a fixture)


def spec(value, status="stated"):
    return {"value": value, "status": status, "evidence": []}


def one(field, s, current=""):
    out = build_proposals({field: s}, {field: current})
    return out[0] if out else None


# ---- proposal builder: every attribute a card or the writeback reads ------------------------------

def test_a_set_proposal_carries_field_property_current_status_and_tentative():
    p = one("budget", spec(60000, "hedged"), "25000")
    assert (p.field, p.prop, p.action, p.current, p.proposed, p.tentative, p.status) == ("budget", "amount", "set", "25000", "60000", True, "hedged")


def test_a_clear_proposal_carries_the_current_value_and_negated_status():
    p = one("champion", spec("Hugo Moreno", "negated"), "Hugo Moreno")
    assert (p.field, p.prop, p.action, p.current, p.proposed, p.tentative, p.status) == ("champion", "ai_champion", "clear", "Hugo Moreno", "", False, "negated")


def test_competitor_and_label_proposals_carry_their_context():
    c = one("competitors", spec([{"name": "Outreach", "stance": "evaluating"}], "hedged"), "Gong (evaluating)")
    assert (c.field, c.prop, c.action, c.current, c.tentative, c.status) == ("competitors", "ai_competitors", "append", "Gong (evaluating)", True, "hedged")
    labels = one("pain_points", spec(["b"], "hedged"), "a")
    assert (labels.field, labels.prop, labels.current, labels.proposed, labels.tentative, labels.status) == ("pain_points", "ai_pain_points", "a", "a; b", True, "hedged")


def test_a_stage_signal_note_has_no_current_value_and_keeps_status():
    p = one("stage_signal", spec("advance", "hedged"), "ignored")
    assert (p.field, p.prop, p.action, p.current, p.proposed, p.tentative, p.status) == ("stage_signal", "note", "note", "", "Stage signal from call: advance", True, "hedged")
    assert one("stage_signal", spec(None)) is None


def test_the_crm_value_for_a_field_defaults_to_empty():
    out = build_proposals({"budget": spec(5)}, {})
    assert out[0].current == "" and out[0].proposed == "5"


def test_competitor_parsing_skips_empty_parts_and_defaults_the_stance():
    assert parse_competitors("A (x);;B (y); ;C") == [{"name": "A", "stance": "x"}, {"name": "B", "stance": "y"}, {"name": "C", "stance": ""}]
    assert merge_competitors([{"name": "A", "stance": "x"}], [{"name": "B"}]) == [{"name": "A", "stance": "x"}, {"name": "B", "stance": ""}]
    assert merge_competitors([{"name": "A", "stance": "x"}], [{"name": "a", "stance": ""}]) == [{"name": "A", "stance": "x"}]  # empty stance keeps the old one


def test_competitor_entries_without_a_name_or_that_are_not_dicts_are_ignored():
    p = one("competitors", spec([{"stance": "evaluating"}, "junk", {"name": "Gong", "stance": "evaluating"}]), "")
    assert p.proposed == "Gong (evaluating)"
    assert one("competitors", spec([{"stance": "evaluating"}, "junk"]), "") is None


def test_next_step_proposals_use_a_lone_dict_or_the_first_listed_step():
    step = {"action": "send_contract", "owner": "Dana", "date": None}
    assert one("next_step", spec(step)).proposed == "send_contract | owner: Dana | by no date"
    assert one("next_step", spec([step, {"action": "x", "owner": "y", "date": None}])).proposed == "send_contract | owner: Dana | by no date"
    assert one("next_step", spec([])) is None and one("next_step", spec("junk")) is None


def test_one_not_mentioned_field_does_not_stop_the_fields_after_it():
    out = build_proposals({"budget": spec(None, "not_mentioned"), "champion": spec("Dev")}, {})
    assert [p.field for out_p in [out] for p in out_p] == ["champion"]
    out = build_proposals({"stage_signal": spec(None), "champion": spec("Dev")}, {})
    assert [p.field for p in out] == ["champion"]


# ---- hand review ----------------------------------------------------------------------------------

def test_choose_calls_is_deterministic_sorted_and_a_subset(monkeypatch):
    ids = [f"d{i:03d}" for i in range(40)]
    monkeypatch.setattr(handreview, "split_ids", lambda paths, split: list(ids))
    a = handreview.choose_calls(None, 10)
    assert a == handreview.choose_calls(None, 10) == sorted(a) and len(set(a)) == 10 and set(a) <= set(ids) and a != ids[:10]
    assert len(handreview.choose_calls(None, 3)) == 3


def test_the_stub_hubspot_reads_stored_crm_text_and_swallows_writes(tmp_path):
    path = tmp_path / "h.sqlite"
    conn = db.connect(path)
    create_proposal(conn, deal_id="d1", hubspot_id="d1", prop="amount", current="25000", proposed="30000", field="budget")
    stub = handreview.StubHubSpot(lambda: db.connect(path))
    assert stub.get_deal("d1", ["amount", "ai_champion"]) == {"amount": "25000", "ai_champion": ""}
    assert stub.get_deal("other", ["amount"]) == {"amount": ""}
    assert stub.patch_deal("d1", {"amount": "9"}) == {} and stub.create_note("d1", "n") == {}
    assert stub.get_deal("d1", ["amount"]) == {"amount": "25000"}  # nothing was written


def test_the_hand_review_report_counts_agreement_by_kind_detail_and_field(tmp_path):
    (tmp_path / "results").mkdir()
    conn = db.connect(tmp_path / "results" / handreview.DB_NAME)
    rows = [("budget", "approved", "", "5"), ("budget", "rejected", "not_crm_worthy", ""), ("champion", "edited", "", "Dev Varga"),
            ("champion", "approved", "", ""), ("use_case", "pending", "", "")]
    pids = []
    for field, status, reason, final in rows:
        pid = create_proposal(conn, deal_id="d1", hubspot_id="d1", prop="p", current="", proposed="x", field=field)
        conn.execute("UPDATE proposals SET review_status=?, reject_reason=?, final_value=? WHERE id=?", (status, reason, final, pid))
        pids.append(pid)
    conn.commit()
    oracle = {str(pids[0]): {"deal": "d1", "kind": "approve", "reason": "", "final": None},
              str(pids[1]): {"deal": "d1", "kind": "reject", "reason": "wrong_value", "final": None},   # same kind, different reason
              str(pids[2]): {"deal": "d2", "kind": "edit", "reason": "", "final": "dev varga"},        # same kind, same text
              str(pids[3]): {"deal": "d2", "kind": "reject", "reason": "wrong_value", "final": None},  # human approved, oracle rejected
              str(pids[4]): {"deal": "d3", "kind": "approve", "reason": "", "final": None}}
    (tmp_path / "results" / handreview.ORACLE_FILE).write_text(json.dumps(oracle))
    out = handreview.report(Paths(tmp_path))
    assert (out["proposals"], out["reviewed_by_human"], out["still_pending"], out["calls"]) == (5, 4, 1, 3)
    assert (out["same_decision_kind"], out["same_decision_and_detail"]) == (3, 2)
    assert out["confusion"] == {"human_approve__oracle_approve": 1, "human_reject__oracle_reject": 1,
                                "human_edit__oracle_edit": 1, "human_approve__oracle_reject": 1}
    assert out["human_decisions"] == {"approve": 2, "reject": 1, "edit": 1} and out["oracle_decisions_same_proposals"] == {"approve": 1, "reject": 2, "edit": 1}
    assert out["human_error_rate_edit_or_reject"] == pytest.approx(0.5) and out["oracle_error_rate_edit_or_reject"] == pytest.approx(0.75)
    assert out["by_field"] == {"budget": {"agree": 2}, "champion": {"agree": 1, "differ": 1}}
    assert json.loads((tmp_path / "results" / "handreview.json").read_text()) == out


def test_the_hand_review_report_with_nothing_reviewed_has_no_rates(tmp_path):
    (tmp_path / "results").mkdir()
    db.connect(tmp_path / "results" / handreview.DB_NAME)
    (tmp_path / "results" / handreview.ORACLE_FILE).write_text("{}")
    out = handreview.report(Paths(tmp_path))
    assert out["reviewed_by_human"] == 0 and out["human_error_rate_edit_or_reject"] is None and out["same_decision_kind"] == 0


# ---- spend statistics -----------------------------------------------------------------------------

def test_call_statistics_use_mean_median_and_the_95th_percentile():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE llm_calls (model TEXT, purpose TEXT, effort TEXT, cost_usd REAL, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER)")
    c.executemany("INSERT INTO llm_calls VALUES ('m','extract','none',?,?,?,?)", [(0.001 * i, 1000 * i, 10 * i, 5 * i) for i in range(1, 21)])
    c.execute("INSERT INTO llm_calls VALUES ('m','extract',NULL,99,99,99,99)")
    s = charts.call_stats(c, "m", "extract", "none")
    assert s["calls"] == 20 and s["mean_cost_usd"] == pytest.approx(0.0105) and s["mean_latency_ms"] == pytest.approx(10500)
    assert s["p50_latency_ms"] == 11000 and s["p95_latency_ms"] == 20000 and s["total_cost_usd"] == pytest.approx(0.21)
    assert s["mean_input_tokens"] == pytest.approx(105) and s["mean_output_tokens"] == pytest.approx(52.5)
    assert charts.call_stats(c, "m", "extract", None)["calls"] == 1  # NULL effort is its own setting
    empty = charts.call_stats(c, "none", "extract", None)
    assert empty["calls"] == 0 and empty["mean_cost_usd"] == 0.0 and empty["p95_latency_ms"] == 0.0


def test_unit_economics_pools_learner_calls_from_both_logs(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "data" / "keys").mkdir(parents=True)
    for name, rows in (("crm.sqlite", [("ext", "extract", "none", 0.002, 4000), ("lrn", "rule-learner", "low", 0.001, 3000)]),
                       ("curve.sqlite", [("lrn", "rule-learner", "low", 0.003, 5000)])):
        c = db.connect(tmp_path / "results" / name)
        for model, purpose, effort, cost, ms in rows:
            c.execute("INSERT INTO llm_calls (model, purpose, effort, cost_usd, latency_ms, input_tokens, output_tokens) VALUES (?,?,?,?,?,?,?)",
                      ("e" if model == "ext" else "l", purpose, effort, cost, ms, 10, 5))
        c.commit()
    h = db.connect(tmp_path / "results" / "handreview.sqlite")
    h.commit()
    out = charts.unit_economics(Paths(tmp_path), "e", "l", "none")
    assert out["extractor"]["calls"] == 1 and out["extractor"]["effort"] == "none" and out["extractor"]["model"] == "e"
    assert out["rule_learner"]["calls"] == 2 and out["rule_learner"]["mean_cost_usd"] == pytest.approx(0.002)
    assert out["rule_learner"]["mean_latency_ms"] == pytest.approx(4000) and out["crm_fill_rate"]["cells"] == 0


# ---- learn-split errors, and which field the gate result is read from -----------------------------

def test_learn_errors_surface_learn_split_mistakes_with_truth_and_prediction(eval_paths):
    deals = json.loads(eval_paths.deals.read_text())
    for d in deals["deals"]:
        if d["deal_id"] == "d002":
            d["split"] = "learn"
    eval_paths.deals.write_text(json.dumps(deals))
    version = extractor.prompt_version(extractor.load_template(eval_paths.root / "prompts" / "extractor_v0.md"))
    rec = {"deal_id": "d002", "fields": {f: {"correct": f != "budget", "score": 1.0 if f != "budget" else 0.0} for f in FIELDS},
           "raw": {f: {"value": "x" if f == "budget" else None, "status": "stated" if f == "budget" else "not_mentioned"} for f in FIELDS}}
    (eval_paths.root / "results").mkdir(exist_ok=True)
    (eval_paths.root / "results" / "learn_runs.json").write_text(json.dumps({f"{version}|luna|none|0|d002": rec, "other-version|x": rec}))
    out = evalcmds.learn_errors(eval_paths, __import__("pathlib").Path("prompts/extractor_v0.md"))
    assert [e["field"] for e in out] == ["budget"] and out[0]["deal"] == "d002" and out[0]["score"] == 0.0
    assert out[0]["pred"] == {"value": "x", "status": "stated"} and "value" in out[0]["truth"] and isinstance(out[0]["cases"], list)


def test_the_learning_summary_reports_the_net_of_the_rules_own_field(setup, monkeypatch):
    paths, conn, pid = setup
    def validate(settings_, conn_, paths_, rule_id, client=None):
        net = {f: 0 for f in FIELDS}
        net["competitors"], net["champion"] = 7, -1
        return {"passed": True, "net_by_field": net, "reasons": [], "validation_score_before": 0.8, "validation_score_after": 0.85}
    monkeypatch.setattr(gate, "validate_rule", validate)
    said = []
    out = learning.learn_and_apply(settings(), paths, pid, said.append, client=FakeSol(GOOD))
    assert out["net"] == 7 and said[-1].endswith("validated: net +7 on competitors")
    v = rules.get_version(conn, rules.current_version(conn)["version_id"])
    assert v["validation_score_before"] == 0.8 and v["validation_score_after"] == 0.85
