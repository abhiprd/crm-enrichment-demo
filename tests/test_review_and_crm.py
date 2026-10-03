import json

import pytest

from crm import db
from crm.config import Settings
from crm.hubspot import HubSpot, fmt_value, props_from_fields
from crm.llm import complete, run_spend
from crm.review import card_blocks, create_proposal, get_proposal, handle_decision


class FakeHS:
    def __init__(self, value):
        self.value, self.writes = value, []

    def get_deal(self, deal_id, props):
        return {"amount": self.value}

    def patch_deal(self, deal_id, props):
        self.writes.append(props)
        self.value = props["amount"]


def make(tmp_path, current="25000"):
    conn = db.connect(tmp_path / "t.sqlite")
    pid = create_proposal(conn, deal_id="d001", hubspot_id="1", prop="amount", current=current, proposed="60000",
                          evidence={"quote": "q", "speaker": "S", "ts": "01:00"})
    return conn, pid


def test_approve_writes_once(tmp_path):
    conn, pid = make(tmp_path)
    hs = FakeHS("25000")
    assert handle_decision(conn, hs, pid, "approve", "U1", ("U1",)).status == "approved"
    assert handle_decision(conn, hs, pid, "approve", "U1", ("U1",)).status == "not_pending"
    assert hs.writes == [{"amount": "60000"}]
    row = get_proposal(conn, pid)
    assert (row["review_status"], row["final_value"], row["reviewed_by"]) == ("approved", "60000", "U1")


def test_unauthorized_click_changes_nothing(tmp_path):
    conn, pid = make(tmp_path)
    hs = FakeHS("25000")
    assert handle_decision(conn, hs, pid, "approve", "U9", ("U1",)).status == "unauthorized"
    assert get_proposal(conn, pid)["review_status"] == "pending" and hs.writes == []


def test_stale_crm_value_blocks_write_and_refreshes(tmp_path):
    conn, pid = make(tmp_path)
    hs = FakeHS("99999")  # someone edited HubSpot after the proposal
    out = handle_decision(conn, hs, pid, "approve", "U1", ())
    row = get_proposal(conn, pid)
    assert out.status == "stale" and hs.writes == []
    assert row["review_status"] == "pending" and row["current_value"] == "99999"


def test_reject_records_reason(tmp_path):
    conn, pid = make(tmp_path)
    assert handle_decision(conn, FakeHS("25000"), pid, "reject", "U1", (), reason="not_crm_worthy").status == "rejected"
    assert get_proposal(conn, pid)["reject_reason"] == "not_crm_worthy"


def test_card_has_buttons_only_while_pending(tmp_path):
    conn, pid = make(tmp_path)
    assert any(b["type"] == "actions" for b in card_blocks(get_proposal(conn, pid)))
    handle_decision(conn, FakeHS("25000"), pid, "approve", "U1", ())
    assert not any(b["type"] == "actions" for b in card_blocks(get_proposal(conn, pid)))


def test_value_formatting():
    assert fmt_value("budget", 60000) == "60000"
    assert fmt_value("budget", {"min": 80000, "max": 100000}) == "100000"
    assert fmt_value("competitors", [{"name": "Gong", "stance": "evaluating"}]) == "Gong (evaluating)"
    assert fmt_value("next_step", {"action": "send_contract", "owner": "A", "date": None}).endswith("no date")
    assert props_from_fields({"budget": None, "pain_points": ["data_quality"]}) == \
        {"amount": "", "ai_pain_points": "data_quality"}


def test_hubspot_dry_run_records_writes_and_makes_no_requests():
    class Boom:
        def request(self, *a, **k):
            raise AssertionError("network call in dry-run")

    hs = HubSpot("", live=False, session=Boom())
    hs.patch_deal("1", {"amount": "5"})
    hs.ensure_properties()
    assert hs.planned[0] == ("PATCH", "/crm/v3/objects/deals/1", {"properties": {"amount": "5"}})
    assert any(p[1] == "/crm/v3/properties/deals/groups" for p in hs.planned)


def test_llm_logs_spend_model_effort_and_reasoning(tmp_path):
    class Details:
        reasoning_tokens = 300

    class Resp:
        class usage:  # noqa: N801
            prompt_tokens, completion_tokens, completion_tokens_details = 1000, 500, Details

        class Msg:
            content = "hi"

        class Choice:
            pass

    Resp.Choice.message = Resp.Msg()
    Resp.choices = [Resp.Choice()]
    seen = {}

    class Client:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    seen.update(kw)
                    return Resp

    s = Settings(extractor_model="luna", prices=(("luna", 2.0, 8.0),))
    conn = db.connect(tmp_path / "t.sqlite")
    res = complete(s, conn, "p", model="luna", effort="low", run_id="r1", purpose="test", client=Client)
    assert seen["model"] == "luna" and seen["reasoning_effort"] == "low"
    assert res.text == "hi" and res.reasoning_tokens == 300 and abs(res.cost_usd - 0.006) < 1e-9
    row = conn.execute("SELECT effort, reasoning_tokens FROM llm_calls").fetchone()
    assert (row["effort"], row["reasoning_tokens"]) == ("low", 300)
    assert abs(run_spend(conn, "r1") - 0.006) < 1e-9
    seen.clear()
    complete(s, conn, "p", model="luna", run_id="r2", purpose="test", client=Client)
    assert "reasoning_effort" not in seen  # omitted when unset


# ---- card context and preflight ------------------------------------------------------------
import json as _json  # noqa: E402

from crm import preflight  # noqa: E402
from crm.hubspot import deal_url  # noqa: E402
from crm.review import record_line  # noqa: E402
from crm.slice import context_for, evidence_for  # noqa: E402

DEAL = {"company": {"name": "Acme Foods"},
        "participants": [{"name": "Dana Reyes", "internal": True}, {"name": "Priya Shah", "internal": False}],
        "hubspot": {"company_id": "111", "deal_id": "222"}}


def test_deal_url_and_record_line():
    assert deal_url(1234567, "222", "app-na2.hubspot.com") == \
        "https://app-na2.hubspot.com/contacts/1234567/record/0-3/222"
    ctx = context_for(DEAL)
    assert record_line(ctx) == "*Acme Foods* (111)  |  Acme Foods - Northbeam (222)"  # no portal id, no link

    class HS:
        def account(self):
            return {"portalId": 9, "uiDomain": "app-na2.hubspot.com"}

    linked = record_line(context_for(DEAL, HS()))
    assert "<https://app-na2.hubspot.com/contacts/9/record/0-3/222|Acme Foods - Northbeam> (222)" in linked


def test_card_shows_account_deal_link_and_quote_speaker(tmp_path):
    class HS:
        def account(self):
            return {"portalId": 9}

    conn = db.connect(tmp_path / "t.sqlite")
    pid = create_proposal(conn, deal_id="d1", hubspot_id="222", prop="amount", current="1", proposed="2",
                          evidence=evidence_for(DEAL), context=context_for(DEAL, HS()))
    blocks = card_blocks(get_proposal(conn, pid))
    text = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "*Acme Foods* (111)" in text and "/record/0-3/222|" in text
    assert "Priya Shah" in text and "Dana Reyes" not in text  # speaker is the external participant


def test_evidence_speaker_is_external_participant():
    assert evidence_for(DEAL)["speaker"] == "Priya Shah"


def test_preflight_flags_missing_scope_and_gates(monkeypatch):
    class R:
        def __init__(self, code, body=None):
            self.status_code, self._b, self.text = code, body or {}, ""

        def json(self):
            return self._b

    def fake(method, url, **kw):
        if "properties/deals/groups" in url and method == "POST":
            return R(403)  # key created without crm.schemas.deals.write
        if "owners" in url:
            return R(200, {"results": [{"id": "1"}]})
        return R(200, {}) if method == "GET" else R(400)

    monkeypatch.setattr(preflight.requests, "request", fake)
    res = preflight.run(Settings(hubspot_key="k", hubspot_owner_id="1"), ["hubspot"])
    by = {c.name: c for c in res["hubspot"]}
    assert by["write property groups"].status == "fail" and "crm.schemas.deals.write" in by["write property groups"].detail
    assert by["read owners"].status == "ok"
    assert not preflight.passed(res)
    assert not preflight.passed(preflight.run(Settings(), ["hubspot"]))  # no key at all


def test_transient_api_errors_are_retried_with_backoff():
    from crm.llm import _create_with_backoff

    class RateLimitError(Exception):
        pass

    class Flaky:
        def __init__(self, fails):
            self.fails, self.calls = fails, 0
            self.chat = self
            self.completions = self

        def create(self, **kw):
            self.calls += 1
            if self.calls <= self.fails:
                raise RateLimitError("429")
            return "ok"

    waits = []
    c = Flaky(2)
    assert _create_with_backoff(c, {}, sleep=waits.append) == "ok" and c.calls == 3 and waits == [2.0, 4.0]
    with pytest.raises(RateLimitError):
        _create_with_backoff(Flaky(99), {}, attempts=3, sleep=lambda s: None)

    class Bad(Exception):
        pass

    class Broken(Flaky):
        def create(self, **kw):
            raise Bad("not transient")

    with pytest.raises(Bad):
        _create_with_backoff(Broken(0), {}, sleep=lambda s: None)
