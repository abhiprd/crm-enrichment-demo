import json

import pytest

from crm import db, rules
from crm.review import create_proposal, handle_decision


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.sqlite")


def cand(conn, field="competitors", text="Record a competitor only if the buyer is evaluating it."):
    return rules.add_candidate(conn, field, text, "because", [1], "U1")


def test_starts_empty_and_activation_adds_a_version(conn):
    v0 = rules.current_version(conn)
    assert v0["change_type"] == "init" and rules.active_rules(conn) == []
    r = cand(conn)
    assert rules.get_rule(conn, r)["status"] == "candidate" and rules.active_rules(conn) == []  # not yet in force
    v1, why = rules.activate(conn, r, "U1", "demo", validated=False)
    assert why == "" and v1 > v0["version_id"]
    assert [x["rule_id"] for x in rules.active_rules(conn)] == [r]
    row = rules.get_rule(conn, r)
    assert row["status"] == "active" and row["validated"] is None  # unvalidated, not failed
    assert rules.get_version(conn, v1)["parent_version_id"] == v0["version_id"]


def test_past_versions_never_change_and_retire_and_revert_add_versions(conn):
    r1, r2 = cand(conn), cand(conn, "champion", "A champion must push for the purchase.")
    v1, _ = rules.activate(conn, r1, "U1", "a")
    v2, _ = rules.activate(conn, r2, "U1", "b")
    snap = json.loads(rules.get_version(conn, v1)["active_rule_ids"])
    assert snap == [r1]  # v1 still lists only r1 after v2 exists
    v3 = rules.retire(conn, r1, "U1", "not helping")
    assert [x["rule_id"] for x in rules.active_rules(conn)] == [r2]
    assert json.loads(rules.get_version(conn, v1)["active_rule_ids"]) == [r1] and rules.get_rule(conn, r1)["status"] == "retired"
    v4 = rules.revert(conn, v1, "U1", "undo")
    assert v4 > v3 and rules.get_version(conn, v4)["change_type"] == "revert"
    assert [x["rule_id"] for x in rules.active_rules(conn)] == [r1]  # back to v1's set, as a NEW version
    assert rules.get_rule(conn, r2)["status"] == "reverted" and rules.get_rule(conn, r1)["status"] == "active"
    assert rules.revert(conn, 9999, "U1", "x") is None


def test_cap_of_five_active_rules_per_field(conn):
    for i in range(5):
        v, why = rules.activate(conn, cand(conn, text=f"rule {i}"), "U1", "x")
        assert why == ""
    sixth = cand(conn, text="rule 6")
    v, why = rules.activate(conn, sixth, "U1", "x")
    assert v is None and "5 active rules" in why and rules.get_rule(conn, sixth)["status"] == "candidate"
    other, why2 = rules.activate(conn, cand(conn, "champion", "c"), "U1", "x")  # another field is unaffected
    assert why2 == "" and other


def test_only_candidates_activate_and_retire_needs_active(conn):
    r = cand(conn)
    rules.activate(conn, r, "U1", "x")
    assert rules.activate(conn, r, "U1", "again")[0] is None
    assert rules.retire(conn, 9999, "U1", "x") is None


def test_version_hash_changes_with_rules_and_validation_is_recorded(conn):
    h0 = rules.current_version(conn)["prompt_template_hash"]
    r = cand(conn)
    v1, _ = rules.activate(conn, r, "U1", "x", validated=False)
    assert rules.get_version(conn, v1)["prompt_template_hash"] != h0
    rules.record_validation(conn, r, True, {"net": 4}, 0.80, 0.82)
    row, ver = rules.get_rule(conn, r), rules.get_version(conn, v1)
    assert row["validated"] == 1 and json.loads(row["gate_result"]) == {"net": 4}
    assert (ver["validation_score_before"], ver["validation_score_after"]) == (0.80, 0.82)


class HS:
    def get_deal(self, deal_id, names):
        return {n: "" for n in names}

    def patch_deal(self, *a):
        pass


def test_examples_are_the_three_most_recent_edits_for_the_field(conn):
    for i in range(4):
        pid = create_proposal(conn, deal_id="d", hubspot_id="1", prop="ai_competitors", current="", proposed=f"p{i}",
                              interaction_id="i", field="competitors", evidence={"quote": f"q{i}"}, context={})
        handle_decision(conn, HS(), pid, "edit", "U1", (), final_value=f"f{i}")
    other = create_proposal(conn, deal_id="d", hubspot_id="1", prop="ai_champion", current="", proposed="x",
                            interaction_id="i", field="champion", evidence={}, context={})
    handle_decision(conn, HS(), other, "edit", "U1", (), final_value="y")
    ex = rules.examples_for(conn, "competitors")
    assert [e["final"] for e in ex] == ["f3", "f2", "f1"] and ex[0]["quote"] == "q3" and ex[0]["proposed"] == "p3"
    assert [e["final"] for e in rules.examples_for(conn, "champion")] == ["y"]


def test_compose_template_inserts_rules_and_examples_before_the_transcript(conn):
    from crm.extractor import compose_template, prompt_version
    tpl = "intro\n\n## Transcript\n\n{{TRANSCRIPT}}"
    assert compose_template(tpl, [], {}) == tpl and prompt_version(compose_template(tpl, [], {"x": []})) == prompt_version(tpl)
    r = cand(conn, text="Record a competitor only if evaluated. {{DATE}}")
    rules.activate(conn, r, "U1", "x")
    out = compose_template(tpl, rules.active_rules(conn),
                           {"competitors": [{"proposed": "A; B", "final": "A", "quote": "we use A"}]})
    assert out.index("## Company conventions") < out.index("## Reviewer corrections") < out.index("## Transcript")
    assert "- competitors: Record a competitor only if evaluated." in out and "{{DATE}}" not in out  # placeholders defused
    assert 'evidence "we use A": a reviewer changed the proposed value `A; B` to `A`' in out
    assert prompt_version(out) != prompt_version(tpl)
    appended = compose_template("no marker here", rules.active_rules(conn))
    assert appended.startswith("no marker here") and "## Company conventions" in appended
