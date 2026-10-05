import json
import sqlite3
import xml.dom.minidom

import pytest

from crm import charts, stats
from crm.paths import Paths


def test_wilson_interval_known_values_and_bounds():
    p, lo, hi = stats.wilson(5, 10)
    assert p == 0.5 and 0.23 < lo < 0.24 and 0.76 < hi < 0.77
    assert stats.wilson(0, 0) == (0.0, 0.0, 1.0)
    assert stats.wilson(0, 20)[1] == 0.0 and stats.wilson(20, 20)[2] == 1.0


def test_spread_separates_overlapping_labels_and_keeps_order():
    out = charts.spread([100.0, 102.0, 130.0], gap=14)
    assert out[1] - out[0] >= 14 and out[2] == 130.0
    assert charts.spread([10.0]) == [10.0]


def curve_fixture():
    pt = lambda b, s, h, t, rules, ex: {"batch": b, "ruleset_version": 1, "active_rule_ids": rules, "examples": ex, "repeats": 3, "calls": 120,  # noqa: E731
                                        "mean_score": s, "by_group": {"house_rule": {"mean": h}, "trap": {"mean": t}}}
    return {"points": [pt(0, 0.8, 0.3, 0.9, [], 0), pt(1, 0.85, 0.6, 1.0, [1], 2)],
            "reviews": [{"batch": 1, "reviewed": 10, "approved": 6, "edited": 1, "rejected": 3,
                         "reject_reasons": {"not_crm_worthy": 2, "wrong_value": 1}},
                        {"batch": 2, "reviewed": 10, "approved": 8, "edited": 0, "rejected": 2,
                         "reject_reasons": {"not_crm_worthy": 1, "wrong_speaker": 1}}],
            "rule_events": [{"status": "created", "gate_passed": True}, {"status": "created", "gate_passed": False},
                            {"status": "skipped"}]}


def test_error_rates_use_edits_plus_rejects_with_wilson_bounds():
    rows = charts.review_error_rates(curve_fixture())
    assert rows[0][:3] == (1, 4, 10) and rows[0][3] == 0.4 and rows[0][4] < 0.4 < rows[0][5]


def test_reason_mix_keeps_top_three_then_folds_the_rest():
    reasons, rows = charts.reason_mix(curve_fixture())
    assert reasons[0] == "not_crm_worthy" and reasons[-1] == "other" and len(reasons) == 4
    assert rows[1][1]["not_crm_worthy"] == 1 and sum(rows[1][1].values()) == 2


def test_rule_inventory_counts_gate_outcomes_and_declines():
    inv = dict(charts.rule_inventory(curve_fixture()))
    assert inv["Candidate rules drafted"] == 2 and inv["Passed the gate, now active"] == 1
    assert inv["Failed the gate, not activated"] == 1 and inv["Learner declined to write a rule"] == 1


def test_charts_are_valid_svg_with_numbers_from_the_data():
    prop = {"v0": {"mean_score": 0.8067}, "manual_baseline": {"mean_score": 0.86}}
    svg = charts.chart_curve(curve_fixture(), prop)
    xml.dom.minidom.parseString(svg)
    assert "0.807" in svg and "0.860" in svg and svg.count("<polyline") == 3
    for fn in (charts.chart_review_error, charts.chart_reasons, charts.chart_rules):
        xml.dom.minidom.parseString(fn(curve_fixture()))
    arm = lambda m, t: {"mean_score": m, "truth_mean_score": t}  # noqa: E731
    test = {"transcripts": 20, "repeats": 5, "arms": {"v0": arm(0.8, 0.9), "manual_baseline": arm(0.85, 0.92), "final": arm(0.91, 0.85)}}
    out = charts.chart_test(test)
    xml.dom.minidom.parseString(out)
    assert "0.910" in out and "0.850" in out
    ab = {"v0": {"run_scores": [0.8, 0.81]}, "arms": {"both": {"run_scores": [0.88, 0.9]}}}
    xml.dom.minidom.parseString(charts.chart_ablation(ab))


def test_call_stats_filters_by_model_purpose_and_effort():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE llm_calls (model TEXT, purpose TEXT, effort TEXT, cost_usd REAL, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER)")
    c.executemany("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?)",
                  [("m", "extract", "none", 0.001, 1000, 10, 5), ("m", "extract", "none", 0.003, 3000, 30, 15), ("m", "extract", "low", 9, 9, 9, 9)])
    s = charts.call_stats(c, "m", "extract", "none")
    assert s["calls"] == 2 and s["mean_cost_usd"] == pytest.approx(0.002) and s["mean_latency_ms"] == 2000


def test_fill_rate_counts_before_and_after_from_reviewer_decisions(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "data" / "keys").mkdir(parents=True)
    fields = ("budget", "decision_timeline", "competitors", "economic_buyer", "champion", "pain_points", "use_case", "next_step")
    key = {"fields": {f: {"crm_before": 5 if f == "budget" else None} for f in fields}}
    (tmp_path / "data" / "keys" / "d1.json").write_text(json.dumps(key))
    c = sqlite3.connect(str(tmp_path / "results" / "handreview.sqlite"))
    c.execute("CREATE TABLE proposals (deal_id TEXT, field TEXT, review_status TEXT, action TEXT)")
    c.executemany("INSERT INTO proposals VALUES (?,?,?,?)", [("d1", "champion", "approved", "set"), ("d1", "use_case", "rejected", "append"),
                                                             ("d1", "budget", "approved", "set")])
    c.commit()
    r = charts.fill_rate(Paths(tmp_path))
    assert (r["cells"], r["filled_before"], r["filled_after"]) == (8, 1, 2)
