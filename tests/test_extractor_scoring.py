import json
from pathlib import Path

import pytest

from crm.extractor import build_prompt, parse_response, prompt_version
from crm.ingest import parse_transcript
from crm.scoring import competitor_f1, field_score, score_field, to_amount
from crm.validator import apply_validation, normalize, quote_supported, validate_fields

FIXTURE = Path(__file__).parent / "fixtures" / "chat_reply_d002.md"


def transcript_block():
    text = FIXTURE.read_text(encoding="utf-8")
    body = text[text.index("Call type"):text.index("--- EVIDENCE")]
    return parse_transcript("Date: 2026-09-18\n" + body)


def test_prompt_has_no_crm_values_or_house_rules():
    p = build_prompt(transcript_block())
    for banned in ("house rule", "crm_before", "not_crm_worthy", "ballpark", "formal evaluation"):
        assert banned not in p.lower()
    assert "{{" not in p and "[11] Elena Brooks" in p
    assert prompt_version() == prompt_version()


def test_parse_response_fills_missing_fields_and_strips_fences():
    ok = {"budget": {"value": 5, "status": "stated", "evidence": [{"idx": 1, "quote": "x"}]},
          "champion": {"value": "A", "status": "not_mentioned", "evidence": []}}
    ex = parse_response("```json\n" + json.dumps({"fields": ok}) + "\n```")
    assert ex.fields["budget"]["value"] == 5
    assert ex.fields["champion"]["value"] is None  # not_mentioned carries no value
    assert ex.fields["use_case"]["status"] == "not_mentioned" and ex.errors
    assert parse_response("nonsense").errors


def test_validator_normalizes_and_drops_unsupported():
    assert normalize("We’re  hiring, now!") == "were hiring now"
    assert quote_supported("were hiring", "We’re hiring, now!")
    utts = {3: "We hired six reps in the spring."}
    fields = {"budget": {"value": 1, "status": "stated", "evidence": [{"idx": 3, "quote": "ten reps"}]},
              "champion": {"value": "A", "status": "stated", "evidence": [{"idx": 3, "quote": "six reps"}]},
              "use_case": {"value": None, "status": "not_mentioned", "evidence": []}}
    bad = validate_fields(fields, utts)
    assert list(bad) == ["budget"]
    assert apply_validation(fields, bad)["budget"]["status"] == "not_mentioned"


def T(v, s="stated"):
    return {"value": v, "status": s}


def test_budget_and_timeline_granularity():
    assert to_amount("$60k") == 60000 and to_amount("60,000") == 60000
    assert score_field("budget", T(60000), T("60k"))["correct"]
    assert not score_field("budget", T({"min": 80000, "max": 100000}, "hedged"), T(100000, "hedged"))["correct"]
    assert score_field("decision_timeline", T("2026-Q4"), T("2026-Q4"))["correct"]
    assert not score_field("decision_timeline", T("2026-Q4"), T("2026-12-31"))["correct"]


def test_status_must_match():
    assert not score_field("budget", T(60000, "hedged"), T(60000, "stated"))["correct"]
    assert score_field("champion", T(None, "not_mentioned"), T(None, "not_mentioned"))["correct"]
    assert not score_field("champion", T(None, "not_mentioned"), T("A B"))["correct"]


def test_competitor_pairs_and_next_step_parts():
    g = [{"name": "Gong", "stance": "evaluating"}]
    assert competitor_f1(g, g) == 1.0
    assert competitor_f1(g, [{"name": "gong", "stance": "incumbent"}]) == 0.0
    both = g + [{"name": "Clari", "stance": "evaluating"}]
    assert abs(competitor_f1(g, both) - 2 / 3) < 1e-9
    ns = {"action": "send_contract", "owner": "Dana Reyes", "date": "2026-09-29"}
    r = score_field("next_step", T(ns), T({**ns, "date": "2026-10-03"}))
    assert abs(r["score"] - 2 / 3) < 1e-9 and r["parts"]["date"] is False


def test_field_score_mean():
    assert field_score({"a": {"score": 1.0}, "b": {"score": 0.5}}) == 0.75


# ---- bake-off ---------------------------------------------------------------------------------
from crm import bakeoff  # noqa: E402
from crm.config import Settings  # noqa: E402
from crm.deals import DealSet  # noqa: E402


def fake_deals():
    ids = sorted(bakeoff.CASE_IDS)
    deals, n = {}, 0
    for i in range(30):
        cases = []
        if i % 5:
            cases = [{"id": ids[n % len(ids)], "field": "budget", "note": "n"}]
            n += 1
        deals[f"d{i:03d}"] = {"deal_id": f"d{i:03d}", "split": "validation" if i < 24 else "learn", "cases": cases}
    return DealSet(vendor={}, deals=deals)


def test_select_deals_covers_cases_with_clean_and_is_deterministic():
    ds = fake_deals()
    a = bakeoff.select_deals(ds)
    assert a == bakeoff.select_deals(ds) and len(a) == 15
    assert all(ds.deals[d]["split"] == "validation" for d in a)
    assert {c["id"] for d in a for c in ds.deals[d]["cases"]} == set(bakeoff.CASE_IDS)
    assert sum(not ds.deals[d]["cases"] for d in a) >= 2


def rec(d, score, correct, cost=0.01):
    return {"deal_id": d, "field_score": score, "fields": {f: {"score": score, "correct": correct}
            for f in bakeoff.FIELDS}, "unsupported": 1, "extracted": 5, "parse_errors": 0,
            "in": 1000, "out": 400, "reasoning": 100, "cost": cost, "ms": 900}


def test_aggregate_spread_and_flips():
    runs = [[rec("a", 0.8, True), rec("b", 0.6, True)], [rec("a", 0.7, True), rec("b", 0.6, False)]]
    agg = bakeoff.aggregate("luna-low", runs)
    assert agg["run_scores"] == pytest.approx([0.7, 0.65]) and abs(agg["score_range"] - 0.05) < 1e-9
    assert agg["flip_rate_by_field"]["budget"] == 0.5 and agg["max_flip_rate"] == 0.5
    assert agg["calls"] == 4 and agg["unsupported"] == 4 and agg["extracted_fields"] == 20


def agg(label, scores, flip, cost):
    return {"setting": label, "repeats": 3, "calls": 45, "run_scores": scores, "mean_score": sum(scores) / 3,
            "score_range": max(scores) - min(scores), "max_flip_rate": flip, "cost_per_call": cost}


def test_choose_cheapest_stable_within_noise_with_headroom():
    aggs = [agg("luna-none", [0.70, 0.71, 0.69], 0.05, 0.001), agg("luna-low", [0.80, 0.81, 0.79], 0.05, 0.003),
            agg("luna-medium", [0.82, 0.83, 0.81], 0.05, 0.010)]
    # none is 0.12 below best (noise 0.02): out. low is 0.02 below best, within noise: cheapest eligible.
    assert bakeoff.choose(aggs)["pick"] == "luna-low"
    aggs[1]["max_flip_rate"] = 0.3  # unstable: falls through to medium
    assert bakeoff.choose(aggs)["pick"] == "luna-medium"
    aggs[2] = agg("luna-medium", [0.97, 0.97, 0.97], 0.0, 0.010)  # at the ceiling: no headroom
    assert bakeoff.choose(aggs)["pick"] is None


def test_plan_is_9_luna_runs_plus_one_sol():
    s = Settings(extractor_model="luna", learner_model="sol")
    p = bakeoff.plan(s, 3)
    assert len(p) == 10 and p[-1][:3] == ("sol", "sol", None)
    assert [x[2] for x in p[:3]] == ["none"] * 3
