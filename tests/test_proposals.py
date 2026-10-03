from crm.proposals import (build_proposals, merge_competitors, parse_competitors, parse_labels)


def spec(value, status="stated"):
    return {"value": value, "status": status, "evidence": []}


def one(field, s, current=""):
    out = build_proposals({field: s}, {field: current})
    return out[0] if out else None


def test_stated_set_and_unchanged_is_none():
    p = one("budget", spec(60000), "25000")
    assert (p.action, p.proposed, p.current, p.tentative, p.prop) == ("set", "60000", "25000", False, "amount")
    assert one("budget", spec(60000), "60000.0") is None  # HubSpot stores 60000.0: same amount
    assert one("economic_buyer", spec("Tom Alvarez"), "tom alvarez") is None
    assert one("economic_buyer", spec("Tom Alvarez"), "Lina Park").action == "set"


def test_hedged_is_tentative_and_superseded_uses_final_value():
    p = one("decision_timeline", spec("2026-Q4", "hedged"), "2026-10-15")
    assert p.tentative and p.proposed == "2026-Q4"
    assert one("budget", spec(65000, "superseded"), "").proposed == "65000"


def test_not_mentioned_never_proposed():
    assert build_proposals({"budget": spec(None, "not_mentioned")}, {"budget": "5"}) == []


def test_negated_single_value_only_clears_when_crm_holds_one():
    assert one("champion", spec(None, "negated"), "") is None
    p = one("champion", spec("Hugo Moreno", "negated"), "Hugo Moreno")
    assert p.action == "clear" and p.proposed == ""


def test_competitors_merge_negation_and_dedupe():
    cur = "Gong (evaluating); Clari (incumbent)"
    assert parse_competitors(cur) == [{"name": "Gong", "stance": "evaluating"}, {"name": "Clari", "stance": "incumbent"}]
    assert one("competitors", spec([{"name": "gong", "stance": "evaluating"}]), cur) is None  # nothing new
    p = one("competitors", spec([{"name": "Outreach", "stance": "evaluating"}]), cur)
    assert p.action == "append" and p.proposed == "Gong (evaluating); Clari (incumbent); Outreach (evaluating)"
    n = one("competitors", spec([{"name": "Clari", "stance": "ruled_out"}], "negated"), cur)  # becomes ruled_out
    assert n.proposed == "Gong (evaluating); Clari (ruled_out)"
    absent = one("competitors", spec([{"name": "Avoma", "stance": "ruled_out"}], "negated"), "")
    assert absent.proposed == "Avoma (ruled_out)"  # added as ruled_out if absent
    assert merge_competitors([], []) == []


def test_labels_append_only_new():
    assert parse_labels("a; b") == ["a", "b"]
    p = one("pain_points", spec(["data_quality", "tool_sprawl"]), "data_quality")
    assert p.action == "append" and p.proposed == "data_quality; tool_sprawl"
    assert one("use_case", spec(["crm_hygiene"]), "crm_hygiene") is None


def test_next_step_list_proposes_first_and_stage_signal_is_a_note():
    steps = [{"action": "send_contract", "owner": "Dana Reyes", "date": "2026-09-29"},
             {"action": "schedule_followup", "owner": "Priya Shah", "date": None}]
    p = one("next_step", spec(steps))
    assert p.proposed == "send_contract | owner: Dana Reyes | by 2026-09-29" and p.prop == "hs_next_step"
    assert one("next_step", spec(steps), p.proposed) is None
    s = one("stage_signal", spec("advance"))
    assert (s.action, s.prop, s.proposed) == ("note", "note", "Stage signal from call: advance")
