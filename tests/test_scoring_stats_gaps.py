"""Tests added after the mutation audit (docs/TEST_AUDIT.md): behaviour in scoring, stats, propscore and the quote
validator that surviving mutants showed nothing asserted. Pinned bootstrap values were produced by the current code
with a fixed seed; the counts, McNemar p-values and score arithmetic are checked against hand calculation."""

import math

import pytest

from crm import propscore, scoring, stats, validator
from crm.proposals import Proposal
from crm.schema import FIELDS


# ---- scoring -------------------------------------------------------------------------------------

def test_vendor_aliases_are_applied_and_unknown_names_are_just_normalised():
    assert scoring.norm_vendor("Gong.io") == "gong"
    assert scoring.norm_vendor("REVENUE.IO") == "revenueio"
    assert scoring.norm_vendor("  Foo   Bar! ") == "foo bar"


@pytest.mark.parametrize("raw,want", [("$50k", 50000), ("50K", 50000), ("1.5M", 1500000), ("2m", 2000000), ("1,200", 1200),
                                      (7.6, 8), (75000, 75000), ("about 90 thousand", 90), ("none", None), (True, None), (None, None)])
def test_to_amount_parses_suffixes_in_either_case(raw, want):
    assert scoring.to_amount(raw) == want


def test_budget_equal_ranges_need_both_bounds_and_a_dict_prediction():
    truth = {"min": 40000, "max": 60000}
    assert scoring.budget_equal(truth, {"min": "$40k", "max": 60000})
    assert not scoring.budget_equal(truth, {"min": 40000, "max": 61000})
    assert not scoring.budget_equal(truth, {"min": 41000, "max": 60000})
    assert not scoring.budget_equal(truth, 60000)
    assert not scoring.budget_equal(50000, {"min": 50000, "max": 50000})
    assert not scoring.budget_equal(50000, 60000) and scoring.budget_equal(50000, "$50k")


@pytest.mark.parametrize("value,want", [("2026-11-30", "2026-Q4"), ("2026-01-01", "2026-Q1"), ("2026-03-31", "2026-Q1"),
                                        ("2026-04-01", "2026-Q2"), ("2026-09-30", "2026-Q3"), ("2026-q3", "2026-Q3"), ("soon", None)])
def test_quarter_of_boundaries(value, want):
    assert scoring.quarter_of(value) == want


def test_timeline_equal_ignores_case_and_needs_exact_day_for_a_date_truth():
    assert scoring.timeline_equal("2026-Q4", "2026-q4")
    assert scoring.timeline_equal("2026-Q4", "2026-10-15") and not scoring.timeline_equal("2026-Q4", "2027-Q1")
    assert scoring.timeline_equal("2026-11-30", "2026-11-30") and not scoring.timeline_equal("2026-11-30", "2026-Q4")
    assert not scoring.timeline_equal("2026-Q4", None)


def test_competitor_pairs_default_to_an_empty_stance():
    assert scoring.pairs([{"name": "Gong"}, "junk"]) == {("gong", "")}


def test_competitor_f1_edge_cases_and_arithmetic():
    a, b, c = ({"name": n, "stance": "evaluating"} for n in "ABC")
    assert scoring.competitor_f1([], []) == 1.0
    assert scoring.competitor_f1([], [a]) == 0.0 and scoring.competitor_f1([a], []) == 0.0
    assert scoring.competitor_f1([a, b], [a, c]) == pytest.approx(0.5)
    assert scoring.competitor_f1([a], [a, b]) == pytest.approx(2 / 3)
    assert scoring.competitor_f1([a, b], [a]) == pytest.approx(2 / 3)
    assert scoring.competitor_f1([a, b], [b, a]) == 1.0


def truth(status, value=None):
    return {"status": status, "value": value}


def test_score_field_status_mismatch_scores_zero_and_not_mentioned_pair_scores_one():
    r = scoring.score_field("budget", truth("stated", 5), truth("hedged", 5))
    assert r == {"score": 0.0, "correct": False, "why": "status"}
    assert scoring.score_field("budget", truth("not_mentioned"), truth("not_mentioned")) == {"score": 1.0, "correct": True}


def test_score_field_single_values_are_exactly_one_or_zero():
    ok = scoring.score_field("stage_signal", truth("stated", "advance"), truth("stated", "ADVANCE"))
    bad = scoring.score_field("stage_signal", truth("stated", "advance"), truth("stated", "hold"))
    assert (ok["score"], ok["correct"]) == (1.0, True) and (bad["score"], bad["correct"]) == (0.0, False)
    for f in ("economic_buyer", "champion"):
        assert scoring.score_field(f, truth("stated", "Dev Varga"), truth("stated", "dev varga"))["correct"]
        assert not scoring.score_field(f, truth("stated", "Dev Varga"), truth("stated", "Pat Lee"))["correct"]


def test_score_field_label_sets_ignore_order_and_reject_non_lists():
    f = "pain_points"
    assert scoring.score_field(f, truth("stated", ["a", "b"]), truth("stated", ["b", "a"]))["correct"]
    assert not scoring.score_field(f, truth("stated", ["a", "b"]), truth("stated", ["a"]))["correct"]
    assert not scoring.score_field(f, truth("stated", ["a"]), truth("stated", "a"))["correct"]


def test_score_field_competitors_credit_is_partial_but_correct_only_when_perfect():
    a, b = {"name": "A", "stance": "evaluating"}, {"name": "B", "stance": "evaluating"}
    part = scoring.score_field("competitors", truth("stated", [a, b]), truth("stated", [a]))
    full = scoring.score_field("competitors", truth("stated", [a]), truth("stated", [a]))
    assert part["score"] == pytest.approx(2 / 3) and part["correct"] is False
    assert full["score"] == 1.0 and full["correct"] is True


def test_score_field_next_step_is_the_mean_of_action_owner_date():
    t = truth("stated", {"action": "send_proposal", "owner": "Dev", "date": "2026-11-02"})
    two = scoring.score_field("next_step", t, truth("stated", {"action": "send_proposal", "owner": "Dev", "date": "2026-11-09"}))
    assert two["score"] == pytest.approx(2 / 3) and two["correct"] is False and two["parts"] == {"action": True, "owner": True, "date": False}
    one = scoring.score_field("next_step", t, truth("stated", {"action": "send_proposal", "owner": "Pat", "date": None}))
    assert one["score"] == pytest.approx(1 / 3)
    full = scoring.score_field("next_step", t, truth("stated", {"action": "SEND_PROPOSAL", "owner": "dev", "date": "2026-11-02"}))
    assert full["score"] == 1.0 and full["correct"] is True
    # A prediction with no usable step earns nothing, even when the key's date is null (None == None used to credit it).
    none = {"action": False, "owner": False, "date": False}
    nodate = {"action": "a", "owner": "o", "date": None}
    for junk in ("junk", None, 5, {}, [], [None, "x", {}]):
        assert scoring.next_step_parts(nodate, junk) == none
    assert scoring.next_step_parts({"action": "a", "owner": "o", "date": "2026-11-02"}, "junk") == none
    # A real step with a null date still matches a null-date key on that part.
    assert scoring.next_step_parts(nodate, {"action": "a", "owner": "x", "date": None}) == {"action": True, "owner": False, "date": True}


def test_a_stated_but_unusable_next_step_scores_zero_not_a_third():
    t = truth("stated", {"action": "send_proposal", "owner": "Dev", "date": None})
    for junk in ("n/a", None, {}, []):
        r = scoring.score_field("next_step", t, truth("stated", junk))
        assert r["score"] == 0.0 and r["correct"] is False and r["parts"] == {"action": False, "owner": False, "date": False}


def test_score_field_rejects_an_unknown_field_by_name():
    with pytest.raises(ValueError, match="bogus"):
        scoring.score_field("bogus", truth("stated", 1), truth("stated", 1))


# ---- stats ---------------------------------------------------------------------------------------

def test_mcnemar_exact_matches_hand_calculation():
    assert stats.mcnemar_exact(0, 0) == 1.0
    assert stats.mcnemar_exact(5, 0) == pytest.approx(2 / 32)
    assert stats.mcnemar_exact(10, 2) == pytest.approx(2 * (1 + 12 + 66) / 4096)
    assert stats.mcnemar_exact(3, 3) == 1.0 and stats.mcnemar_exact(1, 0) == 1.0
    assert stats.mcnemar_exact(2, 10) == stats.mcnemar_exact(10, 2)


def test_paired_bootstrap_is_deterministic_and_pinned():
    assert stats.paired_bootstrap([]) == (0.0, 0.0, 0.0)
    assert stats.paired_bootstrap([0, 1] * 10) == (0.5, 0.3, 0.7)
    m, lo, hi = stats.paired_bootstrap([0.1] * 10)
    assert m == pytest.approx(0.1) and lo == pytest.approx(0.1) and hi == pytest.approx(0.1)
    d = [0, 1, 1, 0, 1, 1, 0, 1] * 3
    assert stats.paired_bootstrap(d) == stats.paired_bootstrap(d, n_boot=10000, seed=0, alpha=0.05)
    assert stats.paired_bootstrap(d) == stats.paired_bootstrap(d)
    wide = stats.paired_bootstrap(d, alpha=0.5)
    assert wide[1] > stats.paired_bootstrap(d)[1] and wide[2] < stats.paired_bootstrap(d)[2]
    assert stats.paired_bootstrap(d)[1] < sum(d) / len(d) < stats.paired_bootstrap(d)[2]


def test_compare_counts_use_a_half_credit_threshold_and_only_shared_instances():
    base = {("a", "f"): [0.0, 0.0], ("b", "f"): [1.0, 1.0], ("c", "f"): [0.5, 0.5], ("d", "f"): [0.4, 0.4], ("e", "f"): [1.0]}
    cand = {("a", "f"): [1.0, 1.0], ("b", "f"): [0.0, 0.0], ("c", "f"): [1.0, 1.0], ("d", "f"): [0.5, 0.5], ("x", "f"): [1.0]}
    out = stats.compare(base, cand, noise_flips=2)
    assert (out["instances"], out["improved"], out["worsened"], out["changed"]) == (4, 2, 1, 3)
    assert out["mean_diff"] == pytest.approx(0.15) and out["mcnemar_p"] == 1.0


def test_clearing_the_noise_floor_needs_p_below_005_and_strictly_more_changes_than_flips():
    base = {(str(i), "f"): [0.0] for i in range(12)}
    cand = {(str(i), "f"): [1.0] for i in range(12)}  # 12 improved, 0 worsened: p = 2/4096
    assert stats.compare(base, cand, noise_flips=11)["clears_noise_floor"] is True
    assert stats.compare(base, cand, noise_flips=12)["clears_noise_floor"] is False  # changed == flips does not clear
    few = stats.compare({(str(i), "f"): [0.0] for i in range(5)}, {(str(i), "f"): [1.0] for i in range(5)}, noise_flips=0)
    assert few["mcnemar_p"] == pytest.approx(0.0625) and few["clears_noise_floor"] is False  # p above 0.05 even with 5 changes
    exact = stats.compare({(str(i), "f"): [0.0] for i in range(6)} | {("w", "f"): [1.0]},
                          {(str(i), "f"): [1.0] for i in range(6)} | {("w", "f"): [0.0]}, noise_flips=0)
    assert exact["improved"] == 6 and exact["worsened"] == 1 and exact["mcnemar_p"] == pytest.approx(0.125)


def test_wilson_matches_a_known_interval():
    p, lo, hi = stats.wilson(8, 10)
    assert p == 0.8 and lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)
    assert stats.wilson(0, 10)[1] == 0.0 and math.isclose(stats.wilson(10, 10)[2], 1.0)


# ---- proposal scoring ----------------------------------------------------------------------------

def spec(value, status="stated"):
    return {"value": value, "status": status, "evidence": [{"idx": 0, "quote": "x"}]}


def test_budget_prediction_must_equal_the_expected_amount_not_just_exist():
    p = propscore.predicted_proposal("budget", spec(60000), None)
    assert not propscore.proposal_correct("budget", {"action": "set", "value": 50000}, p)
    assert propscore.proposal_correct("budget", {"action": "set", "value": 60000}, p)


def test_action_mismatch_is_incorrect_even_when_the_value_matches():
    p = Proposal("champion", "ai_champion", "append", "", "Dev Varga")
    assert not propscore.proposal_correct("champion", {"action": "set", "value": "Dev Varga"}, p)
    assert propscore.proposal_correct("champion", {"action": "append", "value": "Dev Varga"}, p)


def test_score_proposals_diffs_against_the_crm_value_before_the_call():
    key = {f: {"expected_proposal": None, "crm_before": None} for f in FIELDS}
    key["champion"]["crm_before"] = "Dev Varga"
    validated = {f: {"value": None, "status": "not_mentioned", "evidence": []} for f in FIELDS}
    validated["champion"] = spec("Dev Varga")
    out = propscore.score_proposals(key, validated)
    assert out["champion"] == {"correct": True, "score": 1.0}  # already in the CRM: no proposal, none expected
    key["champion"]["crm_before"] = None
    assert propscore.score_proposals(key, validated)["champion"] == {"correct": False, "score": 0.0}
    assert all(v == {"correct": True, "score": 1.0} for f, v in out.items() if f != "champion")


def test_expected_final_text_for_labels_stage_signal_and_plain_values():
    assert propscore.expected_final_text("pain_points", {"action": "append", "value": ["a", "b"]}, ["a"]) == "a; b"
    assert propscore.expected_final_text("stage_signal", {"action": "note", "value": "advance"}, None) == "Stage signal from call: advance"
    assert propscore.expected_final_text("champion", {"action": "set", "value": "Dev Varga"}, "Pat Lee") == "Dev Varga"
    assert propscore.expected_final_text("budget", {"action": "set", "value": 50000}, 40000) == "50000"


# ---- quote validator -----------------------------------------------------------------------------

UTT = {0: "We have fifty thousand dollars", 1: "Gong looks good"}


def ev(idx, quote):
    return {"idx": idx, "quote": quote}


def test_validator_reports_each_failure_reason_and_keeps_scanning_after_a_clean_field():
    fields = {"champion": {"status": "not_mentioned", "evidence": []},
              "budget": {"status": "stated", "value": 5, "evidence": []},
              "competitors": {"status": "stated", "value": [], "evidence": [ev(9, "x")]},
              "use_case": {"status": "stated", "value": [], "evidence": [ev(0, "not in there")]},
              "next_step": {"status": "stated", "value": [], "evidence": [ev(1, "Gong looks good")]}}
    bad = validator.validate_fields(fields, UTT)
    assert bad == {"budget": "no evidence", "competitors": "unknown idx 9", "use_case": "quote not in utterance 0"}


def test_validator_flags_a_field_if_any_of_its_quotes_is_unsupported():
    first_bad = {"x": {"status": "stated", "evidence": [ev(0, "nope"), ev(1, "Gong looks good")]}}
    second_bad = {"x": {"status": "stated", "evidence": [ev(0, "fifty thousand"), ev(1, "nope")]}}
    assert validator.validate_fields(first_bad, UTT) == {"x": "quote not in utterance 0"}
    assert validator.validate_fields(second_bad, UTT) == {"x": "quote not in utterance 1"}
    assert validator.validate_fields({"x": {"status": "stated", "evidence": [ev(0, "Fifty THOUSAND, dollars!")]}}, UTT) == {}
