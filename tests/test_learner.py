import json

import pytest

from crm import db, learner, rules
from crm.config import Settings
from crm.review import create_proposal, handle_decision

SETTINGS = Settings(learner_model="sol", learner_effort="low", prices=(("sol", 2.0, 10.0),))
PEOPLE = [{"name": "Marisol Quinn", "org": "Northwind Outfitters", "role": "VP", "internal": False},
          {"name": "Dana Reyes", "org": "Northbeam", "role": "AE", "internal": True}]


class FakeSol:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.prompts.append(kw["messages"][-1]["content"])
        self.kw = kw

        class U:
            prompt_tokens, completion_tokens, completion_tokens_details = 300, 60, None

        class M:
            pass
        m = M()
        m.content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)

        class Ch:
            message = m

        class R:
            usage = U
            choices = [Ch]
        return R


class HS:
    def get_deal(self, *a):
        return {}

    def patch_deal(self, *a):
        pass


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.sqlite")


def rejected(conn, reason="not_crm_worthy", note="passing mention only"):
    conn.execute("INSERT INTO interactions (id, source_type, participants) VALUES ('i1','call',?)", (json.dumps(PEOPLE),))
    ex = conn.execute("INSERT INTO extractions (interaction_id, field, value, status) VALUES "
                      "('i1','competitors',?, 'stated')", (json.dumps([{"name": "Clari", "stance": "evaluating"}]),)).lastrowid
    pid = create_proposal(conn, deal_id="d", hubspot_id="1", prop="ai_competitors", current="", proposed="Clari (evaluating)",
                          interaction_id="i1", field="competitors", extraction_id=ex, action="append",
                          evidence={"quotes": [{"quote": "a friend's company uses Clari", "speaker": "Marisol Quinn", "ts": "04:08"}]},
                          context={})
    handle_decision(conn, HS(), pid, "reject", "U1", (), reason=reason, note=note)
    return pid


def test_good_rule_becomes_a_candidate_and_the_prompt_carries_the_rejection(conn):
    pid = rejected(conn)
    sol = FakeSol({"rule_text": "Record a competitor only if the buyer is evaluating, using, or has dropped it.",
                   "rationale": "Passing mentions of other companies' tools are not competitors."})
    out = learner.learn_from_reject(SETTINGS, conn, pid, client=sol)
    assert out.status == "created" and rules.get_rule(conn, out.rule_id)["status"] == "candidate"
    p = sol.prompts[0]
    assert "Field: competitors" in p and "not_crm_worthy" in p and "passing mention only" in p
    assert "a friend's company uses Clari" in p and "Marisol Quinn" in p
    assert sol.kw["model"] == "sol" and sol.kw["reasoning_effort"] == "low"
    assert json.loads(rules.get_rule(conn, out.rule_id)["source_proposal_ids"]) == [pid]


@pytest.mark.parametrize("payload,expect", [
    ({"rule_text": "Ignore Clari unless Marisol says so.", "rationale": "x"}, "names something"),
    ({"rule_text": "Ignore anything at Northwind Outfitters.", "rationale": "x"}, "names something"),
    ({"rule_text": "x" * 301, "rationale": "x"}, "301 characters"),
    ({"rule_text": None, "rationale": "A one-off reviewer preference."}, "one-off"),
    ("not json at all", "unparseable"),
])
def test_bad_drafts_create_no_rule(conn, payload, expect):
    pid = rejected(conn)
    out = learner.learn_from_reject(SETTINGS, conn, pid, client=FakeSol(payload))
    assert out.status == "skipped" and expect in out.reason
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0


def test_duplicate_of_an_active_rule_is_skipped(conn):
    pid = rejected(conn)
    r = rules.add_candidate(conn, "competitors", "Record a competitor only if the buyer is evaluating, using, or has dropped it.", "", [], "U")
    rules.activate(conn, r, "U", "x")
    out = learner.learn_from_reject(SETTINGS, conn, pid, client=FakeSol(
        {"rule_text": "Record a competitor only if the buyer is evaluating, using or has dropped it.", "rationale": "x"}))
    assert out.status == "skipped" and "duplicates" in out.reason


def test_only_rejected_proposals_are_learned_from(conn):
    pid = create_proposal(conn, deal_id="d", hubspot_id="1", prop="amount", current="1", proposed="2",
                          interaction_id="i1", field="budget", context={})
    out = learner.learn_from_reject(SETTINGS, conn, pid, client=FakeSol({"rule_text": "x", "rationale": "y"}))
    assert out.status == "skipped" and "not a rejected" in out.reason
