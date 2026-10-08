"""Failure-case tests added after the audit (docs/TEST_AUDIT.md): malformed LLM output, API errors and timeouts,
CRM HTTP errors, empty input, duplicate runs, and the version metadata the rule store records. Fakes are written by
hand, as elsewhere in this suite; nothing here touches the network."""

import json
import sqlite3
from datetime import datetime

import pytest

from crm import db, extractor, hubspot, llm, pipeline, rules
from crm.config import Settings
from crm.hubspot import HubSpot, HubSpotError
from crm.ingest import parse_transcript
from crm.review import create_proposal, get_proposal, handle_decision, proposals_for
from crm.schema import FIELDS
from tests.test_extractor_scoring import transcript_block
from tests.test_pipeline import FakeLLM, SETTINGS as PIPE_SETTINGS, blank, paths, spec  # noqa: F401  (paths is a fixture)
from tests.test_review_flow import HS, make


# ---- LLM backoff and the completion wrapper ---------------------------------------------------------

def _err(name):
    return type(name, (Exception,), {})


class Scripted:
    """chat.completions.create raises or returns each scripted item in turn and records every call."""

    def __init__(self, *items):
        self.items, self.calls = list(items), []
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls.append(kw)
        item = self.items.pop(0) if self.items else self.items_default()
        if isinstance(item, Exception):
            raise item
        return item

    def items_default(self):
        return ok_reply("fallback")


def ok_reply(text="hi", prompt_tokens=100, completion_tokens=50, reasoning=None, content="__text__"):
    class D:
        reasoning_tokens = reasoning

    class U:
        pass
    U.prompt_tokens, U.completion_tokens = prompt_tokens, completion_tokens
    U.completion_tokens_details = D if reasoning is not None else None

    class M:
        pass
    M.content = text if content == "__text__" else content

    class Ch:
        message = M

    class R:
        usage = U
        choices = [Ch]
    return R


@pytest.mark.parametrize("name", ["RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError"])
def test_every_transient_error_type_is_retried_with_exponential_delays(name):
    client, sleeps = Scripted(_err(name)("429"), _err(name)("again"), "done"), []
    assert llm._create_with_backoff(client, {"model": "m"}, sleep=sleeps.append) == "done"
    assert len(client.calls) == 3 and sleeps == [2.0, 4.0]


def test_backoff_delay_is_capped_at_60_seconds_and_attempts_are_bounded():
    sleeps = []
    client = Scripted(*[_err("RateLimitError")("x")] * 8)
    with pytest.raises(Exception, match="x"):
        llm._create_with_backoff(client, {}, attempts=8, sleep=sleeps.append)
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0] and len(client.calls) == 8


def test_default_is_six_attempts_then_the_error_is_raised():
    client, sleeps = Scripted(*[_err("APITimeoutError")("slow")] * 10), []
    with pytest.raises(Exception, match="slow"):
        llm._create_with_backoff(client, {}, sleep=sleeps.append)
    assert len(client.calls) == 6 and len(sleeps) == 5


def test_a_non_transient_error_is_raised_at_once_without_sleeping():
    client, sleeps = Scripted(ValueError("bad request")), []
    with pytest.raises(ValueError):
        llm._create_with_backoff(client, {}, sleep=sleeps.append)
    assert len(client.calls) == 1 and sleeps == []


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.sqlite")


S = Settings(openai_api_key="k", extractor_model="m", prices=(("m", 2.0, 4.0),))


def test_complete_requires_an_api_key_when_no_client_is_given(conn):
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        llm.complete(Settings(), conn, "p", model="m", run_id="r", purpose="x")


def test_complete_builds_messages_effort_cost_and_logs_the_call(conn):
    client = Scripted(ok_reply("text", 1000, 500, reasoning=120))
    res = llm.complete(S, conn, "prompt", model="m", run_id="r1", purpose="extract", effort="low", system="sys", client=client)
    kw = client.calls[0]
    assert kw["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "prompt"}]
    assert kw["reasoning_effort"] == "low" and kw["model"] == "m"
    assert (res.text, res.input_tokens, res.output_tokens, res.reasoning_tokens) == ("text", 1000, 500, 120)
    assert res.cost_usd == pytest.approx((1000 * 2.0 + 500 * 4.0) / 1e6) and res.effort == "low"
    row = conn.execute("SELECT run_id, model, purpose, reasoning_tokens, effort, cost_usd FROM llm_calls").fetchone()
    assert tuple(row) == ("r1", "m", "extract", 120, "low", pytest.approx(0.004))


def test_complete_without_effort_or_system_sends_only_the_user_message(conn):
    client = Scripted(ok_reply(content=None))
    res = llm.complete(S, conn, "p", model="m", run_id="r", purpose="x", client=client)
    assert "reasoning_effort" not in client.calls[0] and client.calls[0]["messages"] == [{"role": "user", "content": "p"}]
    assert res.text == "" and res.reasoning_tokens == 0 and res.effort is None


def test_cost_for_an_unpriced_model_is_zero_and_run_spend_sums_one_run(conn):
    assert llm.cost_usd(S, "other", 1000, 1000) == 0.0
    for run, cost in (("a", 0.5), ("a", 0.25), ("b", 9.0)):
        conn.execute("INSERT INTO llm_calls (run_id, model, cost_usd) VALUES (?,?,?)", (run, "m", cost))
    assert llm.run_spend(conn, "a") == 0.75 and llm.run_spend(conn, "none") == 0.0


def test_model_available_retrieves_the_pinned_model():
    seen = []

    class C:
        class models:
            @staticmethod
            def retrieve(m):
                seen.append(m)
    llm.model_available(S, "pinned", client=C())
    assert seen == ["pinned"]


# ---- extractor: malformed LLM JSON ------------------------------------------------------------------

def test_loads_recovers_json_wrapped_in_prose_or_followed_by_trailing_text():
    assert extractor._loads('Sure! Here it is: {"fields": {"a": 1}} hope that helps') == {"fields": {"a": 1}}
    assert extractor._loads('{"a": 1} trailing words') == {"a": 1}  # start == 0, so the fallback still applies
    assert extractor._loads('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert extractor._loads('x {"a": {"b": [1, {"c": 3}]}} y {') == {"a": {"b": [1, {"c": 3}]}}


@pytest.mark.parametrize("text", ["no braces at all", "} backwards {", "x{", "", "[1, 2"])
def test_loads_raises_when_there_is_no_json_object(text):
    with pytest.raises(json.JSONDecodeError):
        extractor._loads(text)


@pytest.mark.parametrize("text", ["I cannot help with that.", "[]", '"a string"', "", "{not json}"])
def test_parse_response_turns_unusable_output_into_nine_missing_fields(text):
    ex = extractor.parse_response(text)
    assert ex.errors[0].startswith("unparseable JSON") or text in ("[]", '"a string"')
    assert set(ex.fields) == set(FIELDS)
    assert all(v == {"value": None, "status": "not_mentioned", "evidence": []} for v in ex.fields.values())
    assert all(f"{f}: missing or invalid" in ex.errors for f in FIELDS)


def test_parse_response_handles_bad_field_shapes_statuses_and_evidence():
    raw = {"budget": "five", "champion": {"status": "maybe", "value": "x"},
           "competitors": {"status": "stated", "value": [{"name": "A"}],
                           "evidence": [{"idx": 1, "quote": "q"}, {"idx": "2", "quote": "q"}, {"idx": 3}, "junk", {"quote": "q"}]},
           "use_case": {"status": "not_mentioned", "value": ["x"], "evidence": [{"idx": 1, "quote": "q"}]},
           "pain_points": {"status": "stated", "value": ["a"], "evidence": None}}
    ex = extractor.parse_response(json.dumps({"fields": raw}))
    assert ex.fields["budget"]["status"] == "not_mentioned" and "budget: missing or invalid" in ex.errors
    assert ex.fields["champion"] == {"value": None, "status": "not_mentioned", "evidence": []}
    assert ex.fields["competitors"]["evidence"] == [{"idx": 1, "quote": "q"}]
    assert ex.fields["use_case"]["value"] is None  # not_mentioned forces an empty value
    assert ex.fields["pain_points"]["value"] == ["a"] and ex.fields["pain_points"]["evidence"] == []
    assert extractor.parse_response(json.dumps({"fields": []})).errors  # fields is not a dict


def reply(payload):
    return ok_reply(json.dumps(payload) if not isinstance(payload, str) else payload, 100, 50)


def test_extract_gives_up_after_one_retry_and_charges_both_attempts(conn):
    client = Scripted(reply("not json"), reply("still not json"))
    ex = extractor.extract(S, conn, transcript_block(), model="m", effort="low", run_id="r", client=client)
    assert ex.retries == 1 and len(client.calls) == 2
    assert any(e.startswith("unparseable") for e in ex.errors)
    assert all(v["status"] == "not_mentioned" for v in ex.fields.values())
    first_cost = (100 * 2.0 + 50 * 4.0) / 1e6
    assert ex.extra_cost == pytest.approx(first_cost) and ex.llm.cost_usd == pytest.approx(first_cost)
    assert [c.get("reasoning_effort") for c in client.calls] == ["low", "low"]
    n = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose IN ('extract','extract-retry')").fetchone()[0]
    assert n == 2


def test_extract_without_a_problem_makes_one_call_and_records_the_prompt_version(conn):
    client = Scripted(reply({"fields": {}}))
    tpl = "Template {{TRANSCRIPT}}"
    ex = extractor.extract(S, conn, transcript_block(), model="m", effort=None, run_id="r", client=client, template=tpl)
    assert ex.retries == 0 and ex.extra_cost == 0.0 and len(client.calls) == 1
    assert ex.prompt_version == extractor.prompt_version(tpl) and "reasoning_effort" not in client.calls[0]
    assert "[11] Elena Brooks" in client.calls[0]["messages"][-1]["content"]


def test_extract_with_a_good_first_reply_does_not_retry_on_other_errors(conn):
    client = Scripted(reply({"fields": {"budget": "five"}}))
    ex = extractor.extract(S, conn, transcript_block(), model="m", effort=None, run_id="r", client=client)
    assert ex.retries == 0 and len(client.calls) == 1  # a missing field is not an unparseable reply


def test_compose_template_places_rules_before_the_transcript_and_neutralises_braces():
    tpl = "Intro\n## Transcript\nLine\n## Transcript\nAgain"
    out = extractor.compose_template(tpl, [{"field": "champion", "rule_text": "Use {{ this }} rule"}],
                                     {"budget": [{"proposed": "5", "final": "6", "quote": "q"}], "next_step": []})
    first = out.index("## Transcript")
    assert out.index("## Company conventions") < first and out.index("## Reviewer corrections") < first
    assert "- champion: Use { { this } } rule" in out and "{{" not in out
    assert '- budget, evidence "q": a reviewer changed the proposed value `5` to `6`' in out
    assert "next_step" not in out


def test_compose_template_edge_cases():
    assert extractor.compose_template("T", [], {}) == "T" and extractor.compose_template("T", [], None) == "T"
    rule = [{"field": "budget", "rule_text": "r"}]
    assert extractor.compose_template("A {{TRANSCRIPT}} B", rule, {}).startswith("A ## Company conventions")
    out = extractor.compose_template("  head  \n", rule, {})
    assert out.startswith("  head") and out.index("## Company") == len("  head") + 2
    noq = extractor.compose_template("x\n## Transcript", [], {"budget": [{"proposed": "5", "final": "6", "quote": ""}]})
    # Audit finding, pinned as current behaviour: with no quote the line renders "budget,:" (a stray comma).
    assert "- budget,: a reviewer changed the proposed value `5` to `6`" in noq


def test_prompt_version_is_a_short_stable_hash_that_follows_the_text():
    v = extractor.prompt_version("abc")
    assert v.startswith("pv-") and len(v) == 11 and v == extractor.prompt_version("abc") != extractor.prompt_version("abd")
    int(v[3:], 16)


def test_load_template_reads_a_given_path(tmp_path):
    p = tmp_path / "t.md"
    p.write_text("héllo {{TRANSCRIPT}}", encoding="utf-8")
    assert extractor.load_template(p) == "héllo {{TRANSCRIPT}}"
    assert "{{TRANSCRIPT}}" in extractor.load_template()


# ---- empty and malformed transcripts ----------------------------------------------------------------

def test_an_empty_transcript_parses_to_no_utterances_and_a_prompt_still_builds():
    block = parse_transcript("")
    assert block.utterances == [] and block.participants == []
    prompt = extractor.build_prompt(block)
    assert "{{" not in prompt and "[0]" not in prompt
    assert extractor.render_transcript(block) == ""


def test_a_transcript_with_only_a_header_has_no_turns():
    block = parse_transcript("Date: 2026-10-01\nCall type: discovery\nParticipants:\n- A B | Rep | Org | internal\n---\n")
    assert block.utterances == [] and block.date == "2026-10-01"
    assert [p.name for p in block.participants] == ["A B"]


# ---- HubSpot HTTP errors ----------------------------------------------------------------------------

class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


@pytest.mark.parametrize("status", [300, 400, 401, 404, 429, 500, 503])
def test_hubspot_error_status_raises_with_the_code_and_a_truncated_body(status):
    hs = HubSpot("tok", live=True, session=Session(Resp(status, {"x": 1}, "e" * 500)))
    with pytest.raises(HubSpotError) as e:
        hs.call("GET", "/crm/v3/objects/deals/1")
    msg = str(e.value)
    assert f"-> {status}" in msg and "GET /crm/v3/objects/deals/1" in msg and msg.count("e") <= 300 + 5


def test_hubspot_success_sends_bearer_json_and_timeout_and_a_299_is_still_success():
    s = Session(Resp(299, {"ok": True}), Resp(204, None))
    hs = HubSpot("tok", live=True, session=s)
    assert hs.call("PATCH", "/p", {"a": 1}) == {"ok": True}
    assert hs.call("GET", "/q") == {}  # an empty body is {}
    method, url, kw = s.calls[0]
    assert (method, url, kw["json"], kw["timeout"]) == ("PATCH", hubspot.BASE + "/p", {"a": 1}, 30)
    assert kw["headers"] == {"Authorization": "Bearer tok"}


def test_hubspot_live_without_a_token_raises_and_dry_run_only_reads():
    with pytest.raises(HubSpotError, match="HUBSPOT_SERVICE_KEY"):
        HubSpot("", live=True, session=Session()).call("GET", "/x")
    s = Session(Resp(200, {"results": []}))
    hs = HubSpot("tok", live=False, session=s)
    assert hs.call("POST", "/w", {"b": 2}) == {"dry_run": True} and hs.planned == [("POST", "/w", {"b": 2})] and s.calls == []
    hs.call("GET", "/r")
    assert len(s.calls) == 1  # reads still go out in dry-run


def test_account_and_owner_lookups_swallow_hubspot_errors():
    boom = HubSpot("tok", live=True, session=Session(Resp(403, {}, "no scope"), Resp(404, {}, "gone")))
    assert boom.account() == {} and boom.owner_email("77") == ""
    assert HubSpot("tok", live=True, session=Session()).owner_email("") == ""
    ok = HubSpot("tok", live=True, session=Session(Resp(200, {"portalId": 5}), Resp(200, {"email": "a@x.example"}), Resp(200, {"email": None})))
    assert ok.account() == {"portalId": 5} and ok.owner_email("1") == "a@x.example" and ok.owner_email("2") == ""


def test_hubspot_record_helpers_build_the_expected_requests():
    s = Session(Resp(200, {"properties": {"amount": "5"}}), Resp(200, {}), Resp(200, {}), Resp(200, {"results": [{"id": "9"}]}),
                Resp(200, {"results": [{"id": "c1", "properties": {"domain": "a.example"}}]}),
                Resp(200, {"results": [{"id": "d1", "properties": {"dealname": "A deal"}}]}))
    hs = HubSpot("tok", live=True, session=s)
    assert hs.get_deal("22", ["amount", "ai_champion"]) == {"amount": "5"}
    assert s.calls[0][1].endswith("/crm/v3/objects/deals/22?properties=amount,ai_champion")
    hs.patch_deal("22", {"amount": "9"})
    assert s.calls[1][0] == "PATCH" and s.calls[1][2]["json"] == {"properties": {"amount": "9"}}
    hs.create_note("22", "body text")
    note = s.calls[2][2]["json"]
    assert note["properties"]["hs_note_body"] == "body text" and note["properties"]["hs_timestamp"].endswith(".000Z")
    assert note["associations"][0]["to"] == {"id": "22"} and note["associations"][0]["types"][0]["associationTypeId"] == hubspot.NOTE_TO_DEAL
    assert hs.default_owner_id() == "9"
    assert hs.create_companies([{"name": "A", "domain": "a.example"}]) == {"a.example": "c1"}
    assert hs.create_deals([{"name": "A deal", "props": {"amount": "1"}, "company_id": "c1", "owner_id": "9"}]) == {"A deal": "d1"}
    deal = s.calls[5][2]["json"]["inputs"][0]
    assert deal["properties"]["hubspot_owner_id"] == "9" and deal["properties"]["amount"] == "1"
    assert deal["associations"][0]["types"][0]["associationTypeId"] == hubspot.DEAL_TO_COMPANY
    assert HubSpot("tok", live=True, session=Session(Resp(200, {"results": []}))).default_owner_id() == ""


# ---- review edge cases ------------------------------------------------------------------------------

def test_deciding_on_a_missing_proposal_is_an_error(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    out = handle_decision(conn, HS(), 999, "approve", "U1")
    assert (out.status, out.message) == ("error", "proposal 999 not found")


def test_a_rejected_proposal_cannot_be_rejected_edited_or_approved_again(tmp_path):
    conn, pid = make(tmp_path)
    hs = HS(ai_competitors="Gong (evaluating)")
    assert handle_decision(conn, hs, pid, "reject", "UOWNER", reason="wrong_value").status == "rejected"
    for decision, extra in (("reject", {}), ("edit", {"final_value": "x"}), ("approve", {})):
        out = handle_decision(conn, hs, pid, decision, "UOWNER", **extra)
        assert out.status == "not_pending" and out.message == "already rejected"
    row = get_proposal(conn, pid)
    assert row["review_status"] == "rejected" and row["reject_reason"] == "wrong_value" and hs.writes == []


def test_a_second_edit_does_not_overwrite_the_first(tmp_path):
    conn, pid = make(tmp_path)
    hs = HS(ai_competitors="Gong (evaluating)")
    handle_decision(conn, hs, pid, "edit", "UOWNER", final_value="first")
    assert handle_decision(conn, hs, pid, "edit", "UOWNER", final_value="second").status == "not_pending"
    assert get_proposal(conn, pid)["final_value"] == "first" and hs.writes == [{"ai_competitors": "first"}]


def test_a_note_proposal_is_written_without_reading_the_crm_value(tmp_path):
    conn, pid = make(tmp_path, prop="note", field="stage_signal", current="", proposed="Stage signal from call: advance", action="note")

    class NoRead(HS):
        def get_deal(self, *a, **k):
            raise AssertionError("a note must not trigger a stale check")
    hs = NoRead()
    assert handle_decision(conn, hs, pid, "approve", "UOWNER").status == "approved"
    assert hs.notes == ["Stage signal from call: advance"]


def test_numerically_equal_crm_text_is_not_stale(tmp_path):
    conn, pid = make(tmp_path, prop="amount", field="budget", current="25000", proposed="30000", action="set")
    hs = HS(amount="25000.0")  # HubSpot stores amounts as floats
    out = handle_decision(conn, hs, pid, "approve", "UOWNER")
    assert out.status == "approved" and hs.writes == [{"amount": "30000"}]


def test_an_unauthorized_click_cannot_reject_either(tmp_path):
    conn, pid = make(tmp_path)
    out = handle_decision(conn, HS(), pid, "reject", "UINTRUDER", reason="wrong_value")
    assert out.status == "unauthorized" and get_proposal(conn, pid)["review_status"] == "pending"


# ---- duplicates -------------------------------------------------------------------------------------

def test_processing_the_same_call_twice_creates_separate_proposals_without_merging(paths, monkeypatch):
    """Audit finding, pinned as current behaviour: the pipeline does not deduplicate. Re-running one call
    makes a second interaction and a second set of pending proposals for the same deal and field."""
    payload = blank()
    payload["pain_points"] = spec(["rep_ramp_time"], "stated", [(3, "it's taking them five, six months")])
    stamps = iter([datetime(2026, 10, 1, 12, 0, 0), datetime(2026, 10, 1, 12, 0, 1)])

    class Clock:
        @staticmethod
        def now():
            return next(stamps)
    monkeypatch.setattr(pipeline, "datetime", Clock)
    a = pipeline.process_interaction("demo-d02", paths, live=False, settings=PIPE_SETTINGS, llm_client=FakeLLM(payload))
    b = pipeline.process_interaction("demo-d02", paths, live=False, settings=PIPE_SETTINGS, llm_client=FakeLLM(payload))
    assert a["interaction_id"] != b["interaction_id"] and a["proposals"] == b["proposals"] == 1
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    rows = conn.execute("SELECT interaction_id, review_status FROM proposals WHERE field='pain_points'").fetchall()
    assert len(rows) == 2 and {r["review_status"] for r in rows} == {"pending"}


def test_the_same_second_collision_between_two_runs_is_an_integrity_error(paths, monkeypatch):
    """Audit finding, pinned: the interaction id is a timestamp to the second, so two runs of one call within the
    same second collide on the primary key and the second run crashes instead of being de-duplicated."""
    payload = blank()
    same = datetime(2026, 10, 1, 12, 0, 0)

    class Clock:
        @staticmethod
        def now():
            return same
    monkeypatch.setattr(pipeline, "datetime", Clock)
    pipeline.process_interaction("demo-d02", paths, live=False, settings=PIPE_SETTINGS, llm_client=FakeLLM(payload))
    with pytest.raises(sqlite3.IntegrityError):
        pipeline.process_interaction("demo-d02", paths, live=False, settings=PIPE_SETTINGS, llm_client=FakeLLM(payload))


# ---- rule store: version metadata -------------------------------------------------------------------

def test_the_empty_starting_version_and_each_change_record_parent_reason_and_author(conn):
    v1 = rules.current_version(conn)
    assert (v1["change_type"], v1["parent_version_id"], v1["reason"], v1["author"], v1["active_rule_ids"]) == ("init", None, "empty starting ruleset", "system", "[]")
    rid = rules.add_candidate(conn, "champion", "Rule one", "why", [7], "U1")
    ver, why = rules.activate(conn, rid, "alice", "looks right", validated=True)
    v2 = rules.get_version(conn, ver)
    assert why == "" and (v2["change_type"], v2["parent_version_id"], v2["changed_rule_id"], v2["reason"], v2["author"]) == ("add", v1["version_id"], rid, "looks right", "alice")
    assert json.loads(v2["active_rule_ids"]) == [rid] and rules.get_rule(conn, rid)["validated"] == 1 and rules.get_rule(conn, rid)["status"] == "active"
    rv = rules.retire(conn, rid, "bob", "too broad")
    v3 = rules.get_version(conn, rv)
    assert (v3["change_type"], v3["parent_version_id"], v3["changed_rule_id"], v3["reason"], v3["author"]) == ("retire", ver, rid, "too broad", "bob")
    assert rules.retire(conn, rid, "bob", "again") is None  # not active any more
    back = rules.revert(conn, ver, "carol", "oops")
    v4 = rules.get_version(conn, back)
    assert (v4["change_type"], v4["parent_version_id"], v4["reason"], v4["author"]) == ("revert", rv, f"revert to version {ver}: oops", "carol")
    assert json.loads(v4["active_rule_ids"]) == [rid] and rules.get_rule(conn, rid)["status"] == "active"
    assert rules.revert(conn, 999, "carol", "x") is None


def test_active_rules_can_be_read_for_any_past_version(conn):
    a = rules.add_candidate(conn, "champion", "A", "", [], "U")
    b = rules.add_candidate(conn, "budget", "B", "", [], "U")
    va, _ = rules.activate(conn, a, "x", "r")
    vb, _ = rules.activate(conn, b, "x", "r")
    assert [r["rule_id"] for r in rules.active_rules(conn)] == [b, a]  # ordered by field, then id
    assert [r["rule_id"] for r in rules.active_rules(conn, va)] == [a]
    assert rules.get_version(conn, 12345) is None and rules.get_rule(conn, 12345) is None


def test_the_per_field_cap_names_the_field_and_leaves_the_rule_a_candidate(conn):
    for i in range(rules.MAX_ACTIVE_PER_FIELD):
        rules.activate(conn, rules.add_candidate(conn, "champion", f"R{i}", "", [], "U"), "x", "r")
    extra = rules.add_candidate(conn, "champion", "one too many", "", [], "U")
    ver, why = rules.activate(conn, extra, "x", "r")
    assert ver is None and "champion already has 5 active rules" in why and rules.get_rule(conn, extra)["status"] == "candidate"
    assert rules.activate(conn, extra + 100, "x", "r") == (None, "only a candidate rule can be activated")


def test_the_ruleset_hash_follows_field_and_text_and_is_stable(tmp_path):
    def hash_for(n, field, text):
        c = db.connect(tmp_path / f"h{n}.sqlite")
        v, _ = rules.activate(c, rules.add_candidate(c, field, text, "", [], "U"), "x", "r")
        return rules.get_version(c, v)["prompt_template_hash"]
    h = hash_for(1, "champion", "T")
    assert h.startswith("rs-") and len(h) == 11 and h == hash_for(2, "champion", "T")
    assert h != hash_for(3, "budget", "T") and h != hash_for(4, "champion", "U")


def test_validation_scores_land_on_the_adding_version_once_and_only_when_given(conn):
    rid = rules.add_candidate(conn, "champion", "R", "", [], "U")
    ver, _ = rules.activate(conn, rid, "x", "r")
    rules.record_validation(conn, rid, True, {"k": 1})  # no scores: version row untouched
    row = rules.get_version(conn, ver)
    assert row["validation_score_before"] is None and row["validation_score_after"] is None
    rules.record_validation(conn, rid, True, {"k": 1}, score_before=None, score_after=0.7)  # one score is enough
    row = rules.get_version(conn, ver)
    assert row["validation_score_before"] is None and row["validation_score_after"] == 0.7
    rules.record_validation(conn, rid, False, {"k": 2}, score_before=0.5, score_after=0.9)
    row = rules.get_version(conn, ver)
    assert (row["validation_score_before"], row["validation_score_after"]) == (0.5, 0.9)  # first set wins only while "before" is null
    rules.record_validation(conn, rid, False, {"k": 3}, score_before=0.1, score_after=0.2)
    row = rules.get_version(conn, ver)
    assert (row["validation_score_before"], row["validation_score_after"]) == (0.5, 0.9)
    r = rules.get_rule(conn, rid)
    assert r["validated"] == 0 and json.loads(r["gate_result"]) == {"k": 3}


def test_examples_are_the_latest_edits_with_their_first_quote(conn):
    def edit(proposed, final, evidence, when):
        pid = create_proposal(conn, deal_id="d", hubspot_id="1", prop="ai_champion", current="", proposed=proposed, field="champion", evidence=evidence)
        conn.execute("UPDATE proposals SET review_status='edited', final_value=?, reviewed_at=? WHERE id=?", (final, when, pid))
    edit("A", "A2", {"quotes": [{"quote": "first"}, {"quote": "second"}]}, "2026-10-01T00:00:01")
    edit("B", "B2", {"quote": "single"}, "2026-10-01T00:00:02")
    edit("C", "C2", {}, "2026-10-01T00:00:03")
    edit("D", "D2", {"quotes": [{"quote": "newest"}]}, "2026-10-01T00:00:04")
    conn.commit()
    got = rules.examples_for(conn, "champion", 3)
    assert [g["proposed"] for g in got] == ["D", "C", "B"] and [g["quote"] for g in got] == ["newest", "", "single"]
    assert rules.examples_for(conn, "champion", 4)[-1] == {"proposed": "A", "final": "A2", "quote": "first"}
    assert rules.examples_for(conn, "budget") == []
