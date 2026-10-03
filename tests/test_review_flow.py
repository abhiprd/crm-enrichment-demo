import json

from crm import db
from crm.review import (card_blocks, create_proposal, edit_modal, get_proposal, handle_decision, modal_values,
                        proposals_for, refresh_stale, reject_modal, reviewers_for)


class HS:
    def __init__(self, **props):
        self.props, self.writes, self.notes = dict(props), [], []

    def get_deal(self, deal_id, names):
        return {n: self.props.get(n, "") for n in names}

    def patch_deal(self, deal_id, props):
        self.writes.append(props)
        self.props.update(props)

    def create_note(self, deal_id, body):
        self.notes.append(body)


def make(tmp_path, prop="ai_competitors", field="competitors", current="Gong (evaluating)",
         proposed="Gong (evaluating); Clari (ruled_out)", action="append", reviewers=("UOWNER", "UMGR"),
         extraction=None):
    conn = db.connect(tmp_path / "t.sqlite")
    ex_id = None
    if extraction:
        cur = conn.execute("INSERT INTO interactions (id, source_type) VALUES ('i1','call')")
        cur = conn.execute("INSERT INTO extractions (interaction_id, field, value, status) VALUES ('i1',?,?,?)",
                           (field, json.dumps(extraction["value"]), extraction["status"]))
        ex_id = cur.lastrowid
    pid = create_proposal(conn, deal_id="d1", hubspot_id="222", prop=prop, current=current, proposed=proposed,
                          action=action, interaction_id="i1", field=field, extraction_id=ex_id,
                          evidence={"quote": "q", "speaker": "S", "ts": "01:00"},
                          context={"account_name": "Acme", "reviewers": list(reviewers)})
    return conn, pid


def test_approve_writes_merged_text_and_authorization_comes_from_context(tmp_path):
    conn, pid = make(tmp_path)
    assert reviewers_for(get_proposal(conn, pid)) == ("UOWNER", "UMGR")
    hs = HS(ai_competitors="Gong (evaluating)")
    assert handle_decision(conn, hs, pid, "approve", "UINTRUDER").status == "unauthorized" and hs.writes == []
    out = handle_decision(conn, hs, pid, "approve", "UMGR")  # a manager may act
    assert out.status == "approved" and hs.writes == [{"ai_competitors": "Gong (evaluating); Clari (ruled_out)"}]
    assert handle_decision(conn, hs, pid, "approve", "UOWNER").status == "not_pending" and len(hs.writes) == 1


def test_edit_writes_final_value_and_records_it(tmp_path):
    conn, pid = make(tmp_path)
    hs = HS(ai_competitors="Gong (evaluating)")
    assert handle_decision(conn, hs, pid, "edit", "UOWNER", final_value="").status == "error"  # empty edit refused
    out = handle_decision(conn, hs, pid, "edit", "UOWNER", final_value="Gong (incumbent)")
    row = get_proposal(conn, pid)
    assert out.status == "edited" and hs.writes == [{"ai_competitors": "Gong (incumbent)"}]
    assert (row["review_status"], row["final_value"]) == ("edited", "Gong (incumbent)")


def test_clear_note_and_reject_with_reason_and_note(tmp_path):
    conn, pid = make(tmp_path, prop="ai_champion", field="champion", current="Hugo", proposed="", action="clear")
    hs = HS(ai_champion="Hugo")
    assert handle_decision(conn, hs, pid, "approve", "UOWNER").status == "approved" and hs.writes == [{"ai_champion": ""}]
    conn2, pid2 = make(tmp_path / "n", prop="note", field="stage_signal", current="", proposed="Stage signal: advance",
                       action="note") if (tmp_path / "n").mkdir() is None else (None, None)
    hs2 = HS()
    assert handle_decision(conn2, hs2, pid2, "approve", "UOWNER").status == "approved"
    assert hs2.notes == ["Stage signal: advance"] and hs2.writes == []  # a note, never a property write
    conn3, pid3 = make(tmp_path / "r") if (tmp_path / "r").mkdir() is None else (None, None)
    out = handle_decision(conn3, HS(), pid3, "reject", "UOWNER", reason="not_crm_worthy", note="passing mention")
    row = get_proposal(conn3, pid3)
    assert out.status == "rejected" and (row["reject_reason"], row["note"]) == ("not_crm_worthy", "passing mention")


def test_stale_rebuilds_from_extraction_or_closes(tmp_path):
    ext = {"value": [{"name": "Clari", "stance": "ruled_out"}], "status": "negated"}
    conn, pid = make(tmp_path, extraction=ext)
    hs = HS(ai_competitors="Gong (evaluating); Avoma (evaluating)")  # someone edited HubSpot after the proposal
    out = handle_decision(conn, hs, pid, "approve", "UOWNER")
    row = get_proposal(conn, pid)
    assert out.status == "stale" and hs.writes == [] and row["review_status"] == "pending"
    assert row["proposed_value"] == "Gong (evaluating); Avoma (evaluating); Clari (ruled_out)"  # re-diffed
    # the CRM already holds the proposed value: the proposal is closed, nothing written
    conn2, pid2 = make(tmp_path / "x", extraction=ext) if (tmp_path / "x").mkdir() is None else (None, None)
    assert refresh_stale(conn2, pid2, "Gong (evaluating); Clari (ruled_out)").status == "resolved"
    assert get_proposal(conn2, pid2)["review_status"] == "rejected"


def test_card_has_per_field_buttons_and_no_approve_all(tmp_path):
    conn, pid = make(tmp_path)
    create_proposal(conn, deal_id="d1", hubspot_id="222", prop="amount", current="1", proposed="2",
                    interaction_id="i1", field="budget", context={"account_name": "Acme"})
    blocks = card_blocks(proposals_for(conn, "i1"), owner_mention="<@UOWNER>")
    acts = [b for b in blocks if b["type"] == "actions"]
    assert len(acts) == 2 and all([e["action_id"] for e in a["elements"]] == ["approve", "edit", "reject"] for a in acts)
    flat = json.dumps(blocks).lower()
    assert "approve all" not in flat and "approve_all" not in flat
    assert "2 proposed updates" in blocks[0]["text"]["text"] and "<@UOWNER>" in blocks[0]["text"]["text"]
    handle_decision(conn, HS(ai_competitors="Gong (evaluating)"), pid, "approve", "UOWNER")
    assert len([b for b in card_blocks(proposals_for(conn, "i1")) if b["type"] == "actions"]) == 1  # one resolved


def test_modals_round_trip(tmp_path):
    conn, pid = make(tmp_path)
    row = get_proposal(conn, pid)
    em = edit_modal(row, "C1", "123.4")
    assert em["blocks"][0]["element"]["initial_value"] == row["proposed_value"]
    vals = modal_values({"private_metadata": em["private_metadata"],
                         "state": {"values": {"value": {"text": {"value": "new text"}}}}})
    assert (vals["pid"], vals["channel"], vals["ts"], vals["text"]) == (pid, "C1", "123.4", "new text")
    rm = reject_modal(row, "C1", "123.4")
    assert [o["value"] for o in rm["blocks"][0]["element"]["options"]] == [
        "wrong_value", "wrong_speaker", "stale_or_superseded", "hedged_not_committed", "not_crm_worthy",
        "evidence_doesnt_support"]
    rv = modal_values({"private_metadata": rm["private_metadata"], "state": {"values": {
        "reason": {"choice": {"selected_option": {"value": "wrong_speaker"}}}, "note": {"text": {"value": "x"}}}}})
    assert (rv["reason"], rv["note"]) == ("wrong_speaker", "x")


def test_card_shows_every_cited_quote_with_speaker_and_time(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    quotes = [{"quote": "We're on Clari today.", "speaker": "Marisol Quinn", "ts": "03:40", "idx": 13},
              {"quote": "we're actively comparing Gong right now.", "speaker": "Marisol Quinn", "ts": "04:08", "idx": 15},
              {"quote": "we're not looking at Avoma anymore.", "speaker": "Marisol Quinn", "ts": "04:08", "idx": 15}]
    create_proposal(conn, deal_id="d1", hubspot_id="222", prop="ai_competitors", current="Clari (incumbent)",
                    proposed="Clari (incumbent); Gong (evaluating); Avoma (ruled_out)", action="append",
                    interaction_id="i1", field="competitors", evidence={"quotes": quotes},
                    context={"account_name": "Acme"})
    text = "\n".join(b["text"]["text"] for b in card_blocks(proposals_for(conn, "i1")) if b["type"] == "section")
    assert "comparing Gong" in text and "Avoma anymore" in text and "on Clari today" in text
    assert text.count("Marisol Quinn") == 3
    many = [{"quote": f"q{i}", "speaker": "S", "ts": "00:0%d" % i, "idx": i} for i in range(6)]
    create_proposal(conn, deal_id="d1", hubspot_id="222", prop="amount", current="1", proposed="2",
                    interaction_id="i2", field="budget", evidence={"quotes": many}, context={})
    t2 = "\n".join(b["text"]["text"] for b in card_blocks(proposals_for(conn, "i2")) if b["type"] == "section")
    assert "q3" in t2 and "q4" not in t2 and "+2 more cited lines" in t2
