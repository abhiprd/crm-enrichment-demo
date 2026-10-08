"""Third audit batch (docs/TEST_AUDIT.md): the review card and modals, decision messages, gate arithmetic and
verdicts, learner thresholds and prompt, and the oracle's logged rows. Each assertion is on exact output."""

import json
import math
import sqlite3

import pytest

from crm import db, gate, learner, oracle, rules
from crm.config import Settings
from crm.extractor import Extraction
from crm.review import (MAX_QUOTES, REJECT_REASONS, _final_line, _label, _now, _proposal_text, card_blocks,
                        create_proposal, edit_modal, get_proposal, handle_decision, modal_values, proposals_for,
                        record_line, refresh_stale, reject_modal, reviewers_for)
from crm.schema import FIELDS
from tests.test_gate import RuleSensitiveModel, repo as gate_repo  # noqa: F401  (repo is a fixture)
from tests.test_learner import FakeSol, HS as NullHS, PEOPLE, SETTINGS as LEARNER_SETTINGS, rejected


class Recorder:
    """HubSpot stand-in that records the record id every call used."""

    def __init__(self, **props):
        self.props, self.gets, self.patches, self.notes = dict(props), [], [], []

    def get_deal(self, deal_id, names):
        self.gets.append(deal_id)
        return {n: self.props.get(n, "") for n in names}

    def patch_deal(self, deal_id, props):
        self.patches.append((deal_id, props))
        self.props.update(props)

    def create_note(self, deal_id, body):
        self.notes.append((deal_id, body))


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.sqlite")


def proposal(conn, **kw):
    base = dict(deal_id="d1", hubspot_id="222", prop="ai_competitors", current="Gong (evaluating)", proposed="Gong (evaluating); Clari (incumbent)",
                action="append", field="competitors", evidence={}, context={"reviewers": ["UOWNER"]})
    base.update(kw)
    return get_proposal(conn, create_proposal(conn, **base))


# ---- create_proposal ------------------------------------------------------------------------------

def test_create_proposal_defaults_and_field_inference(conn):
    row = proposal(conn, field="", prop="amount", evidence=None, context=None, tentative=False)
    assert (row["tentative"], row["evidence"], row["context"], row["field"], row["review_status"]) == (0, "{}", "{}", "budget", "pending")
    assert proposal(conn, field="", prop="note")["field"] == "stage_signal"
    assert proposal(conn, field="champion", prop="ai_champion", tentative=True, channel="C1", ruleset_version_id=4)["tentative"] == 1
    r = get_proposal(conn, proposal(conn, channel="C9", ruleset_version_id=4, interaction_id="i7")["id"])
    assert (r["slack_channel"], r["ruleset_version_id"], r["interaction_id"], r["hubspot_object"]) == ("C9", 4, "i7", "deal")
    assert [x["id"] for x in proposals_for(conn, "i7")] == [r["id"]]


def test_the_default_for_tentative_is_false(conn):
    pid = create_proposal(conn, deal_id="d", hubspot_id="1", prop="amount", current="", proposed="5")
    assert get_proposal(conn, pid)["tentative"] == 0 and get_proposal(conn, pid)["action"] == "set"


# ---- card text ------------------------------------------------------------------------------------

def test_labels_come_from_the_field_or_the_property(conn):
    assert _label(proposal(conn, field="decision_timeline")) == "Decision timeline"
    assert _label(proposal(conn, field="", prop="ai_champion")) == "Champion"
    assert _label(proposal(conn, field="", prop="mystery_prop")) == "Mystery prop"


def test_proposal_text_for_set_clear_note_and_tentative(conn):
    s = _proposal_text(proposal(conn, field="budget", prop="amount", action="set", current="", proposed="9", tentative=True))
    assert s.startswith("*Budget*   (empty) → 9 (tentative)")
    assert _proposal_text(proposal(conn, field="budget", prop="amount", action="set", current="5", proposed="9")) .startswith("*Budget*   5 → 9")
    assert _proposal_text(proposal(conn, field="champion", prop="ai_champion", action="clear", current="Dev", proposed="")).startswith("*Champion*   Dev → (clear)")
    assert _proposal_text(proposal(conn, field="champion", prop="ai_champion", action="clear", current="", proposed="")).startswith("*Champion*   (empty) → (clear)")
    note = _proposal_text(proposal(conn, field="stage_signal", prop="note", action="note", current="", proposed="Stage signal from call: hold", tentative=True))
    assert note.startswith("*Stage signal*   add note: Stage signal from call: hold (tentative)")


def test_quotes_show_speaker_time_truncate_at_400_and_stop_after_four(conn):
    quotes = [{"quote": "q%d" % i, "speaker": "Dana", "ts": "01:0%d" % i} for i in range(6)]
    text = _proposal_text(proposal(conn, evidence={"quotes": quotes}))
    assert MAX_QUOTES == 4 and text.count("> \"q") == 4 and "(+2 more cited lines)" in text
    assert '> "q0"\n> — Dana, 01:00' in text and "q4" not in text
    long = _proposal_text(proposal(conn, evidence={"quotes": [{"quote": "x" * 500, "speaker": "", "ts": ""}]}))
    assert "x" * 400 in long and "x" * 401 not in long and "—" in long
    old = _proposal_text(proposal(conn, evidence={"quote": "older format", "speaker": "Pat", "ts": "00:10"}))
    assert '> "older format"\n> — Pat, 00:10' in old
    bare = _proposal_text(proposal(conn, evidence={"quotes": [{"quote": "no speaker"}]}))
    assert '> "no speaker"' in bare and "(+" not in bare
    assert "more cited" not in _proposal_text(proposal(conn, evidence={"quotes": quotes[:4]}))


def test_the_final_line_names_who_acted_and_what_happened(conn):
    base = proposal(conn)
    def with_(**cols):
        conn.execute("UPDATE proposals SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), base["id"]))
        return _final_line(get_proposal(conn, base["id"]))
    assert with_(review_status="approved", reviewed_by="U1", final_value="Gong") == "Approved by <@U1>: Gong"
    assert with_(review_status="approved", reviewed_by="system", final_value="") == "Approved by the system: (cleared)"
    assert with_(review_status="approved", reviewed_by=None, final_value="x") == "Approved by the system: x"
    assert with_(review_status="edited", reviewed_by="U2", final_value="New") == "Edited by <@U2>: New"
    assert with_(review_status="rejected", reviewed_by="U3", reject_reason="wrong_value") == "Rejected by <@U3> (wrong_value)"
    assert with_(review_status="no_response") == "no_response"


def test_the_card_header_buttons_and_final_lines(conn):
    ctx = {"account_name": "Acme", "account_id": "A1", "deal_name": "Big deal", "deal_id": "D1", "deal_url": "https://hs.example/d1",
           "owner_slack_id": "UOWN", "reviewers": ["UOWN"]}
    a = proposal(conn, context=ctx, interaction_id="i1")
    b = proposal(conn, context=ctx, interaction_id="i1", field="budget", prop="amount", action="set", current="", proposed="9")
    handle_decision(conn, Recorder(ai_competitors="Gong (evaluating)"), a["id"], "approve", "UOWN")
    blocks = card_blocks(proposals_for(conn, "i1"))
    header = blocks[0]["text"]["text"]
    assert header.startswith("<@UOWN> *Acme* (A1)  |  <https://hs.example/d1|Big deal> (D1)") and header.endswith("2 proposed updates")
    actions = [x for x in blocks if x["type"] == "actions"]
    assert len(actions) == 1 and actions[0]["block_id"] == f"proposal_{b['id']}"
    ids = [(e["action_id"], e["value"], e.get("style")) for e in actions[0]["elements"]]
    assert ids == [("approve", str(b["id"]), "primary"), ("edit", str(b["id"]), None), ("reject", str(b["id"]), "danger")]
    finals = [x["elements"][0]["text"] for x in blocks if x["type"] == "context"]
    assert finals == ["Approved by <@UOWN>: Gong (evaluating); Clari (incumbent)"]
    assert [x["type"] for x in blocks].count("divider") == 2
    assert "1 proposed update" in card_blocks(get_proposal(conn, b["id"]))[0]["text"]["text"] and "updates" not in card_blocks(b)[0]["text"]["text"]
    assert card_blocks(b, owner_mention="<@UX>")[0]["text"]["text"].startswith("<@UX> ")
    plain = proposal(conn, context=None)  # no context: no record line, just the count
    assert card_blocks(plain)[0]["text"]["text"] == "1 proposed update"


def test_record_line_has_safe_defaults():
    assert record_line({}) == "*Unknown account* (?)  |  Unknown deal (?)"
    assert record_line({"account_name": "A", "account_id": "1", "deal_name": "D", "deal_id": "2"}) == "*A* (1)  |  D (2)"


def test_edit_and_reject_modals(conn):
    row = proposal(conn, field="competitors", proposed="Gong (evaluating)")
    m = edit_modal(row, "C1", "1.2")
    assert m["callback_id"] == "edit_submit" and json.loads(m["private_metadata"]) == {"pid": row["id"], "channel": "C1", "ts": "1.2"}
    assert m["submit"]["text"] == "Save and write" and m["title"]["text"] == "Edit Competitors"
    el = m["blocks"][0]["element"]
    assert (m["blocks"][0]["block_id"], el["action_id"], el["initial_value"], el["multiline"], m["blocks"][0]["optional"]) == ("value", "text", "Gong (evaluating)", False, False)
    long = proposal(conn, field="", prop="a_very_long_property_name_here")
    assert len(edit_modal(long, "C", "t")["title"]["text"]) == 24 and len(reject_modal(long, "C", "t")["title"]["text"]) == 24
    note = proposal(conn, field="stage_signal", prop="note", action="note", proposed="n")
    assert edit_modal(note, "C", "t")["blocks"][0]["element"]["multiline"] is True
    r = reject_modal(row, "C1", "1.2")
    opts = r["blocks"][0]["element"]
    assert [o["value"] for o in opts["options"]] == list(REJECT_REASONS) and opts["initial_option"]["value"] == REJECT_REASONS[0]
    assert r["callback_id"] == "reject_submit" and r["submit"]["text"] == "Reject" and r["blocks"][1]["optional"] is True
    assert json.loads(r["private_metadata"]) == {"pid": row["id"], "channel": "C1", "ts": "1.2"} and r["title"]["text"] == "Reject Competitors"


def test_modal_values_read_the_submitted_state_and_default_when_absent():
    view = {"private_metadata": json.dumps({"pid": 3, "channel": "C", "ts": "9"}),
            "state": {"values": {"value": {"text": {"value": "typed"}}, "reason": {"choice": {"selected_option": {"value": "wrong_value"}}},
                                 "note": {"text": {"value": "why"}}}}}
    assert modal_values(view) == {"pid": 3, "channel": "C", "ts": "9", "text": "typed", "reason": "wrong_value", "note": "why"}
    assert modal_values({}) == {"text": None, "reason": "", "note": ""}
    assert modal_values({"state": {"values": {"note": {"text": {"value": None}}}}})["note"] == ""


# ---- decisions: messages, record ids, stale handling -----------------------------------------------

def test_each_outcome_has_its_message_and_every_hubspot_call_uses_the_record_id(conn):
    hs = Recorder(ai_competitors="Gong (evaluating)")
    p = proposal(conn)
    assert handle_decision(conn, hs, p["id"], "approve", "UX") .message == "only the owner can review this"
    out = handle_decision(conn, hs, p["id"], "approve", "UOWNER")
    assert out.message == "wrote ai_competitors=Gong (evaluating); Clari (incumbent)" and hs.gets == ["222"] and hs.patches[0][0] == "222"
    assert handle_decision(conn, hs, p["id"], "approve", "UOWNER").message == "already approved"
    cleared = proposal(conn, field="champion", prop="ai_champion", action="clear", current="Dev", proposed="")
    out = handle_decision(conn, Recorder(ai_champion="Dev"), cleared["id"], "approve", "UOWNER")
    assert out.message == "wrote ai_champion=(cleared)"
    rejected_ = proposal(conn)
    assert handle_decision(conn, hs, rejected_["id"], "reject", "UOWNER", reason="wrong_value", note="n").message == "rejected"
    row = get_proposal(conn, rejected_["id"])
    assert (row["review_status"], row["reject_reason"], row["note"], row["reviewed_by"]) == ("rejected", "wrong_value", "n", "UOWNER") and row["reviewed_at"]
    default = proposal(conn)
    handle_decision(conn, hs, default["id"], "reject", "UOWNER")
    assert get_proposal(conn, default["id"])["reject_reason"] == "wrong_value"
    empty = proposal(conn)
    assert handle_decision(conn, hs, empty["id"], "edit", "UOWNER", final_value="  ").message == "an edited value cannot be empty"
    assert handle_decision(conn, hs, empty["id"], "edit", "UOWNER", final_value=None).message == "an edited value cannot be empty"


def test_a_note_is_written_to_the_record_with_its_text(conn):
    hs = Recorder()
    p = proposal(conn, field="stage_signal", prop="note", action="note", current="", proposed="Stage signal from call: hold")
    out = handle_decision(conn, hs, p["id"], "approve", "UOWNER")
    assert hs.notes == [("222", "Stage signal from call: hold")] and out.message == "added note on the deal: Stage signal from call: hold"


def test_a_cleared_edit_may_be_empty_for_a_clear_action(conn):
    p = proposal(conn, field="champion", prop="ai_champion", action="clear", current="Dev", proposed="")
    out = handle_decision(conn, Recorder(ai_champion="Dev"), p["id"], "edit", "UOWNER", final_value="")
    assert out.status == "edited" and get_proposal(conn, p["id"])["final_value"] == ""


def test_stale_messages_and_the_refreshed_values(conn):
    p = proposal(conn, prop="amount", field="budget", action="set", current="5", proposed="9")
    out = handle_decision(conn, Recorder(amount="7"), p["id"], "approve", "UOWNER")
    assert out.status == "stale" and out.message == "CRM value changed to 7; card refreshed, nothing written"
    assert get_proposal(conn, p["id"])["current_value"] == "7" and get_proposal(conn, p["id"])["review_status"] == "pending"
    blank = proposal(conn, prop="amount", field="budget", action="set", current="5", proposed="9")
    assert refresh_stale(conn, blank["id"], "").message == "CRM value changed to (empty); card refreshed, nothing written"
    conn.execute("INSERT INTO interactions (id, source_type) VALUES ('ix','call')")
    ex = conn.execute("INSERT INTO extractions (interaction_id, field, value, status) VALUES ('ix','budget',?,?)", ("9", "stated")).lastrowid
    done = proposal(conn, prop="amount", field="budget", action="set", current="5", proposed="9", extraction_id=ex)
    out = refresh_stale(conn, done["id"], "9")
    assert out.status == "resolved" and out.message == "the CRM already holds the proposed value; proposal closed"
    row = get_proposal(conn, done["id"])
    assert (row["review_status"], row["reject_reason"], row["reviewed_by"], row["current_value"]) == ("rejected", "stale_or_superseded", "system", "9")
    moved = proposal(conn, prop="amount", field="budget", action="set", current="5", proposed="9", extraction_id=ex)
    refresh_stale(conn, moved["id"], "4")
    assert get_proposal(conn, moved["id"])["proposed_value"] == "9" and get_proposal(conn, moved["id"])["current_value"] == "4"
    conn.execute("UPDATE extractions SET value=NULL, status='stated' WHERE id=?", (ex,))
    nulled = proposal(conn, prop="amount", field="budget", action="set", current="5", proposed="9", extraction_id=ex)
    assert refresh_stale(conn, nulled["id"], "4").status == "resolved"  # no value left to propose


def test_reviewers_default_to_nobody_and_timestamps_are_utc_seconds(conn):
    assert reviewers_for(proposal(conn, context=None)) == () and reviewers_for(proposal(conn)) == ("UOWNER",)
    stamp = _now()
    assert stamp.endswith("+00:00") and "." not in stamp and len(stamp) == 25


# ---- gate -----------------------------------------------------------------------------------------

ALLOW = {f: 1.0 for f in FIELDS}


def flat(**c):
    return {f: c.get(f, {"improved": 0, "worsened": 0}) for f in FIELDS}


def test_a_net_gain_equal_to_the_allowance_is_not_enough():
    equal = gate.decide(flat(competitors={"improved": 4, "worsened": 1}), "competitors", {**ALLOW, "competitors": 3.0})
    assert not equal["passed"] and equal["reasons"] == ["competitors net gain 3 does not exceed its noise allowance 3.0"]
    assert gate.decide(flat(competitors={"improved": 5, "worsened": 1}), "competitors", {**ALLOW, "competitors": 3.0})["passed"]
    low = gate.decide(flat(competitors={"improved": 2, "worsened": 0}), "competitors", ALLOW)
    assert low["reasons"][0] == f"competitors net gain 2 is below the minimum {gate.MIN_NET_GAIN}"
    loss = gate.decide(flat(competitors={"improved": 9, "worsened": 0}, champion={"improved": 0, "worsened": 3}), "competitors", ALLOW)
    assert loss["reasons"] == [f"champion lost 3 net instances (limit {gate.MAX_OTHER_LOSS})"] and loss["net_by_field"]["champion"] == -3


def runs(*per_run):
    out = []
    for o in per_run:
        by = {}
        for (d, f), ok in o.items():
            by.setdefault(d, {})[f] = ok
        out.append([{"deal_id": d, "fields": {f: {"correct": by[d].get(f, True), "score": 1.0} for f in FIELDS}} for d in by])
    return out


@pytest.mark.parametrize("n,correct,want", [(1, 1, True), (1, 0, False), (2, 1, True), (2, 0, False), (3, 1, False), (3, 2, True),
                                            (4, 1, False), (4, 2, True), (5, 2, False), (5, 3, True)])
def test_a_majority_needs_ceil_half_for_even_and_more_than_half_for_odd_run_counts(n, correct, want):
    outcomes = [{("a", "budget"): i < correct} for i in range(n)]
    assert gate.majority_correct(runs(*outcomes))[("a", "budget")] is want


def test_net_changes_sum_over_instances_and_ignore_those_missing_from_either_side():
    base = runs({("a", "budget"): False, ("b", "budget"): False, ("c", "budget"): True, ("only_base", "budget"): False})
    cand = runs({("a", "budget"): True, ("b", "budget"): True, ("c", "budget"): False, ("only_cand", "budget"): True})
    ch = gate.net_changes(base, cand)
    assert ch["budget"] == {"improved": 2, "worsened": 1}
    assert sum(v["improved"] + v["worsened"] for v in ch.values()) == 3


def test_field_allowance_scales_flip_rates_and_names_the_missing_file(tmp_path):
    from crm.paths import Paths
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "v0_noise.json").write_text(json.dumps({"report": {"flip_rate_by_field": {"budget": 0.1, "champion": None}}}))
    (tmp_path / "results" / "proposal_scores.json").write_text(json.dumps({"v0": {"flip_rate_by_field": {"budget": 0.25}}}))
    p = Paths(tmp_path)
    assert gate.field_allowance(p, 40)["budget"] == pytest.approx(4.0) and gate.field_allowance(p, 40)["champion"] == 0.0
    assert gate.field_allowance(p, 40, "pfields")["budget"] == pytest.approx(10.0) and set(gate.field_allowance(p, 8)) == set(FIELDS)
    (tmp_path / "results" / "proposal_scores.json").unlink()
    with pytest.raises(SystemExit, match=r"results/proposal_scores.json is missing.*run `crm eval rescore`"):
        gate.field_allowance(p, 40, "pfields")
    (tmp_path / "results" / "v0_noise.json").unlink()
    with pytest.raises(SystemExit) as e:
        gate.field_allowance(p, 40)
    assert str(e.value) == "results/v0_noise.json is missing: the gate needs the measured noise floor"


def candidate(conn, field="competitors"):
    return rules.add_candidate(conn, field, "Only count competitors the buyer is evaluating.", "why", [1], "U")


def test_verdicts_activate_retire_or_hold_with_the_author_reason_and_stored_result(conn):
    result = {"passed": True, "net_by_field": {"competitors": 4}, "reasons": [], "validation_score_before": 0.8, "validation_score_after": 0.85}
    rid = candidate(conn)
    assert gate.apply_verdict(conn, rid, result, author="curve-gate") == "validated: net +4 on competitors"
    v = rules.current_version(conn)
    assert (v["change_type"], v["changed_rule_id"], v["author"], v["reason"]) == ("add", rid, "curve-gate", "passed the validation gate")
    # Audit finding, pinned as current behaviour: apply_verdict records the scores before it activates a candidate, so the
    # version row created by that activation has no validation scores (they only land when the rule was already active).
    assert (v["validation_score_before"], v["validation_score_after"]) == (None, None)
    r = rules.get_rule(conn, rid)
    assert (r["status"], r["validated"]) == ("active", 1) and json.loads(r["gate_result"]) == {"net_by_field": {"competitors": 4}, "reasons": []}
    active = candidate(conn, "champion")
    rules.activate(conn, active, "u", "demo", validated=False)
    before = rules.current_version(conn)["version_id"]
    assert gate.apply_verdict(conn, active, {**result, "net_by_field": {"champion": 5}}) == "validated: net +5 on champion"
    assert rules.current_version(conn)["version_id"] == before and rules.get_rule(conn, active)["validated"] == 1
    added = conn.execute("SELECT * FROM ruleset_versions WHERE changed_rule_id=? AND change_type='add'", (active,)).fetchone()
    assert (added["validation_score_before"], added["validation_score_after"]) == (0.8, 0.85)  # an already-active rule gets its scores
    bad = {"passed": False, "net_by_field": {"champion": -1}, "reasons": ["too low", "other lost"], "validation_score_before": 0.8, "validation_score_after": 0.7}
    msg = gate.apply_verdict(conn, active, bad, author="gate-bot")
    assert msg == "retired automatically: too low; other lost" and rules.get_rule(conn, active)["status"] == "retired"
    v = rules.current_version(conn)
    assert (v["change_type"], v["author"], v["reason"]) == ("retire", "gate-bot", "failed the validation gate: too low; other lost")
    held = candidate(conn, "budget")
    assert gate.apply_verdict(conn, held, {**bad, "net_by_field": {"budget": 0}}) == "stays a candidate: too low; other lost"
    assert rules.get_rule(conn, held)["status"] == "candidate" and rules.get_rule(conn, held)["validated"] == 0


def test_templates_for_the_gate_differ_only_by_the_candidate_rule(conn):
    other = candidate(conn, "champion")
    rules.activate(conn, other, "u", "r")
    cand_id = candidate(conn, "budget")
    pid = create_proposal(conn, deal_id="d", hubspot_id="1", prop="amount", current="", proposed="5", field="budget", evidence={"quote": "qq"})
    conn.execute("UPDATE proposals SET review_status='edited', final_value='6', reviewed_at='2026-01-01' WHERE id=?", (pid,))
    conn.commit()
    base, cand = gate._templates(conn, rules.get_rule(conn, cand_id))
    assert "- champion: Only count competitors" in base and "- budget:" not in base.split("## Reviewer corrections")[0]
    assert "- budget: Only count competitors" in cand and "- champion: Only count competitors" in cand
    assert "changed the proposed value `5` to `6`" in base and "changed the proposed value `5` to `6`" in cand


def test_validate_rule_sends_the_extractor_effort_and_writes_to_the_named_file(gate_repo):
    conn = db.connect(gate_repo.root / "results" / "crm.sqlite")
    rid = rules.add_candidate(conn, "stage_signal", "Judge stage by what the call says, not by tone.", "why", [1], "U")
    seen = []

    class Spy(RuleSensitiveModel):
        def create(self, **kw):
            seen.append(kw)
            return super().create(**kw)
    settings = Settings(extractor_model="luna", extractor_effort="low", prices=(("luna", 0.1, 0.5),))
    res = gate.validate_rule(settings, conn, gate_repo, rid, client=Spy(), metric="fields", out_name="sub/dir/gate_x.json")
    assert seen and all(kw["reasoning_effort"] == "low" for kw in seen)
    assert (gate_repo.root / "results" / "sub" / "dir" / "gate_x.json").exists() and res["metric"] == "fields"
    assert (res["transcripts"], res["repeats"], res["thresholds"]) == (1, gate.REPEATS, {"min_net_gain": gate.MIN_NET_GAIN, "max_other_loss": gate.MAX_OTHER_LOSS})
    assert res["field"] == "stage_signal" and res["rule_text"].startswith("Judge stage") and res["cost_usd"] > 0
    assert set(res["changes"]) == set(FIELDS) and set(res["truth_changes"]) == set(FIELDS) and set(res["allowance"]) == set(FIELDS)
    default = gate.validate_rule(settings, conn, gate_repo, rid, client=RuleSensitiveModel())
    assert default["metric"] == "pfields" and (gate_repo.root / "results" / f"gate_{rid}.json").exists()


# ---- learner --------------------------------------------------------------------------------------

def test_rule_checks_cover_blank_length_names_and_duplicates():
    people = PEOPLE
    assert learner.check_rule(None, people, []) == "no rule proposed" and learner.check_rule("   ", people, []) == "no rule proposed"
    assert learner.check_rule("x" * 300, people, []) == "" and learner.check_rule("x" * 301, people, []) == "rule is 301 characters (limit 300)"
    assert learner.check_rule("Ask Marisol first", people, []) == "rule names something from the call (Marisol); it must be general"
    assert "Northwind Outfitters" in learner.check_rule("Skip Northwind Outfitters", people, [])
    assert learner.check_rule("Quinnipiac is fine", people, []) == ""  # a surname inside a longer word is not a leak
    assert learner.check_rule("quinn said so", people, []).startswith("rule names")
    existing = [{"rule_text": "alpha bravo charlie delta"}]
    assert learner.check_rule("alpha bravo charlie delta echo", people, existing) == "duplicates a rule already in force"
    assert learner.check_rule("totally unrelated words appear here", people, existing) == ""


def test_name_leaks_check_names_orgs_and_surname_tokens_and_tolerate_missing_keys():
    assert learner.name_leaks("see Dana Reyes at Northbeam", PEOPLE) == ["Dana", "Dana Reyes", "Northbeam", "Reyes"]
    assert learner.name_leaks("nothing here", PEOPLE) == [] and learner.name_leaks("anything", [{}]) == []
    assert learner.name_leaks("reyesville", PEOPLE) == [] and learner.name_leaks("REYES", PEOPLE) == ["Reyes"]
    assert learner.name_leaks("abc", [{"name": "Al Bo", "org": ""}]) == []  # tokens under four letters are ignored


def test_duplicate_detection_uses_a_jaccard_of_at_least_point_eight():
    a, b = "alpha bravo charlie delta", "alpha bravo charlie delta echo"  # 4 of 5 words shared: exactly 0.8
    assert learner.is_duplicate(a, [{"rule_text": b}]) is True
    assert learner.is_duplicate("alpha bravo charlie delta echo foxtrot", [{"rule_text": b}]) is True   # 5 of 6 shared: 0.83
    assert learner.is_duplicate("alpha bravo charlie delta foxtrot golf", [{"rule_text": b}]) is False  # 4 of 7 shared: 0.57
    assert learner.is_duplicate("alpha bravo xx", [{"rule_text": "yy zz"}]) is False and learner.is_duplicate("", [{"rule_text": b}]) is False
    assert learner.is_duplicate(a, []) is False


def test_the_learner_prompt_carries_every_part_of_the_rejection(conn):
    pid = rejected(conn)
    row = get_proposal(conn, pid)
    extraction = conn.execute("SELECT value, status FROM extractions").fetchone()
    tpl = "F={{FIELD}}|S={{STATUS}}|V={{VALUE}}|C={{CURRENT}}|R={{REASON}}|M={{REASON_MEANING}}|N={{NOTE}}|Q={{QUOTES}}|E={{EXISTING}}"
    out = learner.build_prompt(row, extraction, [{"rule_text": "Existing rule"}], tpl)
    assert out == ('F=competitors|S=stated|V=[{"name": "Clari", "stance": "evaluating"}]|C=(empty)|R=not_crm_worthy|'
                   'M=true, but not something the company records in the CRM|N=passing mention only|'
                   'Q=  - "a friend\'s company uses Clari" (Marisol Quinn, 04:08)|E=- Existing rule')
    plain = proposal(conn, proposed="Clari (evaluating)", current="", evidence=None, context=None, interaction_id="i1")
    handle_decision(conn, NullHS(), plain["id"], "reject", "U1", (), reason="wrong_value", note="")
    bare = learner.build_prompt(get_proposal(conn, plain["id"]), None, [], tpl)
    assert "S=stated" in bare and "V=Clari (evaluating)" in bare and "N=(none)" in bare and "E=(none)" in bare
    conn.execute("UPDATE proposals SET evidence='{}', current_value='Gong (evaluating)' WHERE id=?", (pid,))
    again = learner.build_prompt(get_proposal(conn, pid), extraction, [], tpl)
    assert "Q=  (none)" in again and "C=Gong (evaluating)" in again
    conn.execute("UPDATE proposals SET evidence=? WHERE id=?", (json.dumps({"quote": "single", "ts": "01:00"}), pid))
    assert '- "single" (?, 01:00)' in learner.build_prompt(get_proposal(conn, pid), extraction, [], tpl)
    assert "{{FIELD}}" not in learner.build_prompt(row, extraction, [])  # the shipped template has them all filled


def test_learn_from_reject_reports_why_it_skipped(conn):
    s = LEARNER_SETTINGS
    assert learner.learn_from_reject(s, conn, 999) == learner.LearnResult("skipped", "proposal is not a rejected one")
    pid = rejected(conn)
    conn.execute("UPDATE proposals SET review_status='pending' WHERE id=?", (pid,))
    assert learner.learn_from_reject(s, conn, pid).reason == "proposal is not a rejected one"
    conn.execute("UPDATE proposals SET review_status='rejected', reject_reason='bogus' WHERE id=?", (pid,))
    assert learner.learn_from_reject(s, conn, pid) == learner.LearnResult("skipped", "proposal has no learnable field or reason")
    conn.execute("UPDATE proposals SET reject_reason='not_crm_worthy', field='nonsense' WHERE id=?", (pid,))
    assert learner.learn_from_reject(s, conn, pid).status == "skipped"
    conn.execute("UPDATE proposals SET field='competitors' WHERE id=?", (pid,))
    assert learner.learn_from_reject(s, conn, pid, client=FakeSol("not json")) == learner.LearnResult("skipped", "the learner returned unparseable output")
    assert learner.learn_from_reject(s, conn, pid, client=FakeSol({"rule_text": "", "rationale": "company-specific"})).reason == "company-specific"
    assert learner.learn_from_reject(s, conn, pid, client=FakeSol({"rule_text": "", "rationale": ""})).reason == "no rule proposed"
    assert learner.learn_from_reject(s, conn, pid, client=FakeSol(["a", "list"])).reason == "no rule proposed"
    assert learner.learn_from_reject(s, conn, pid, client=FakeSol({"rule_text": "Skip Clari-like Marisol"})).reason.startswith("rule names something")


def test_a_created_rule_records_its_source_author_and_rationale(conn):
    pid = rejected(conn)
    sol = FakeSol({"rule_text": "  Record a competitor only if the buyer evaluates it.  ", "rationale": "because"})
    res = learner.learn_from_reject(LEARNER_SETTINGS, conn, pid, client=sol)
    assert res.status == "created" and res.reason == "" and res.rule_text == "Record a competitor only if the buyer evaluates it."
    row = rules.get_rule(conn, res.rule_id)
    assert (row["field"], row["rationale"], row["created_by"], row["status"]) == ("competitors", "because", "U1", "candidate")
    assert json.loads(row["source_proposal_ids"]) == [pid]
    assert sol.kw["model"] == "sol" and sol.kw["reasoning_effort"] == "low"
    assert conn.execute("SELECT run_id, purpose FROM llm_calls").fetchone()[:] == (f"learn-{pid}", "rule-learner")
    again = learner.learn_from_reject(LEARNER_SETTINGS, conn, pid, client=FakeSol({"rule_text": "x"}))
    assert again.status == "created"  # a candidate is not "in force", so it is not a duplicate of itself
    rules.activate(conn, res.rule_id, "u", "r")
    dup = learner.learn_from_reject(LEARNER_SETTINGS, conn, pid, client=FakeSol({"rule_text": "Record a competitor only if the buyer evaluates it."}))
    assert dup.reason == "duplicates a rule already in force"


# ---- oracle ---------------------------------------------------------------------------------------

def test_oracle_decisions_carry_reason_and_final_text_and_the_superset_boundary():
    from crm.propscore import predicted_proposal
    gong = {"name": "Gong", "stance": "evaluating"}
    outreach = {"name": "Outreach", "stance": "mentioned"}
    key = lambda truth_status, expected, before=None: {"truth": {"value": 1, "status": truth_status}, "expected_proposal": expected, "crm_before": before}  # noqa: E731
    spec = lambda v, st="stated": {"value": v, "status": st, "evidence": []}  # noqa: E731
    case = [{"id": "competitor_threshold", "field": "competitors"}]
    exact = predicted_proposal("competitors", spec([gong]), None)
    assert oracle.decide("competitors", key("stated", {"action": "append", "value": [gong]}), exact, case).kind == "approve"
    sup = predicted_proposal("competitors", spec([gong, outreach]), None)
    d = oracle.decide("competitors", key("stated", {"action": "append", "value": [gong]}), sup, case)
    assert (d.kind, d.reason, d.final) == ("reject", "not_crm_worthy", None)
    other = predicted_proposal("competitors", spec([outreach]), None)  # neither equal nor a superset: edit to the truth
    e = oracle.decide("competitors", key("stated", {"action": "append", "value": [gong]}), other, case)
    assert (e.kind, e.reason, e.final) == ("edit", "", "Gong (evaluating)")
    none = oracle.decide("competitors", key("negated", None), exact, [])
    assert (none.kind, none.reason) == ("reject", "evidence_doesnt_support")
    hedged = oracle.decide("competitors", key("hedged", None), exact, [])
    assert hedged.reason == "hedged_not_committed"


def test_review_extraction_logs_the_interaction_extractions_and_proposal_rows(tmp_path):
    from crm.paths import Paths
    conn = db.connect(tmp_path / "c.sqlite")
    People = type("P", (), {})
    def person(name, org, role, internal):
        p = People(); p.name, p.org, p.role, p.internal = name, org, role, internal; return p
    utt = type("U", (), {"idx": 0, "speaker": "Dev", "role": "VP", "text": "we have fifty thousand dollars ballpark", "seconds": 65})()
    block = type("B", (), {"date": "2026-10-01", "participants": [person("Dev Varga", "Acme", "VP", False), person("Pat", "Us", "AE", True)], "utterances": [utt]})()
    fields = {f: {"value": None, "status": "not_mentioned", "evidence": []} for f in FIELDS}
    fields["budget"] = {"value": 50000, "status": "stated", "evidence": [{"idx": 0, "quote": "fifty thousand dollars"}]}
    ex = Extraction(fields=fields, prompt_version="pv-x")
    key = {"fields": {f: {"truth": {"value": None, "status": "not_mentioned"}, "expected_proposal": None, "crm_before": None} for f in FIELDS}}
    res = oracle.review_extraction(conn, Paths(tmp_path), "d9", block, ex, key, [{"id": "ballpark_budget", "field": "budget"}], 7, "model-x")
    assert res["interaction_id"] == "d9@curve7"
    inter = conn.execute("SELECT * FROM interactions").fetchone()
    assert (inter["id"], inter["source_type"], inter["occurred_at"], inter["deal_ref"]) == ("d9@curve7", "call", "2026-10-01", "d9")
    assert json.loads(inter["participants"]) == [{"name": "Dev Varga", "org": "Acme", "role": "VP", "internal": False}, {"name": "Pat", "org": "Us", "role": "AE", "internal": True}]
    rows = conn.execute("SELECT field, value, status, prompt_version, model, ruleset_version_id FROM extractions ORDER BY id").fetchall()
    assert len(rows) == 9 and rows[0]["field"] == "budget" and all(r["model"] == "model-x" and r["ruleset_version_id"] == 7 for r in rows)
    assert [r["field"] for r in rows if r["status"] == "not_mentioned" and r["value"] == "null"] == [f for f in FIELDS if f != "budget"]
    budget = conn.execute("SELECT * FROM extractions WHERE field='budget'").fetchone()
    assert (budget["value"], budget["status"], budget["prompt_version"], budget["model"], budget["ruleset_version_id"]) == ("50000", "stated", "pv-x", "model-x", 7)
    assert json.loads(budget["evidence"]) == {"evidence": [{"idx": 0, "quote": "fifty thousand dollars"}]}
    prop = conn.execute("SELECT * FROM proposals").fetchone()
    assert (prop["extraction_id"], prop["deal_id"], prop["hubspot_id"], prop["field"], prop["ruleset_version_id"], prop["interaction_id"], prop["review_status"], prop["reviewed_by"]) == (budget["id"], "d9", "d9", "budget", 7, "d9@curve7", "rejected", "oracle")
    assert json.loads(prop["evidence"])["quotes"][0] == {"quote": "fifty thousand dollars", "speaker": "Dev", "ts": "01:05", "idx": 0}
    assert json.loads(prop["context"]) == {"reviewers": ["oracle"]} and res["decisions"][0]["proposal_id"] == prop["id"]


def test_the_oracle_stub_hubspot_starts_from_the_crm_text_and_records_writes():
    stub = oracle.StubHubSpot({"budget": 25000, "champion": "Dev"})
    assert stub.get_deal("x", ["amount", "ai_champion", "ai_use_case"]) == {"amount": "25000", "ai_champion": "Dev", "ai_use_case": ""}
    stub.patch_deal("x", {"amount": "9"})
    stub.create_note("x", "a note")
    assert stub.get_deal("x", ["amount"]) == {"amount": "9"} and stub.writes == [("patch", {"amount": "9"}), ("note", "a note")]
