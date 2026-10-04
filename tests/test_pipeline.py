import json
import shutil
from pathlib import Path

import pytest

from crm import db, pipeline
from crm.config import Settings
from crm.ingest import ingest_inbox
from crm.paths import Paths
from crm.review import proposals_for

REPO = Path(__file__).resolve().parents[1]
SETTINGS = Settings(extractor_model="luna", extractor_effort="none", learner_model="sol",
                    slack_reviewer_ids=("UREV",), slack_manager_ids=("UMGR",), prices=(("luna", 0.1, 0.5),))


class FakeLLM:
    """Replies with a fixed extraction for the d002 demo call."""

    def __init__(self, payload):
        self.payload = payload
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.prompts = getattr(self, "prompts", []) + [kw["messages"][-1]["content"]]

        class U:
            prompt_tokens, completion_tokens, completion_tokens_details = 100, 50, None

        class M:
            pass
        m = M()
        m.content = json.dumps({"fields": self.payload})

        class Ch:
            message = m

        class R:
            usage = U
            choices = [Ch]
        return R


def spec(value, status="stated", evidence=()):
    return {"value": value, "status": status, "evidence": [{"idx": i, "quote": q} for i, q in evidence]}


def blank():
    return {f: spec(None, "not_mentioned") for f in ["budget", "decision_timeline", "competitors", "economic_buyer",
                                                       "champion", "pain_points", "use_case", "next_step", "stage_signal"]}


@pytest.fixture
def paths(tmp_path) -> Paths:
    (tmp_path / "data").mkdir()
    deals = json.loads((REPO / "tests" / "fixtures" / "deals.json").read_text())
    for d in deals["deals"]:  # pretend d002 (Harborvine) is seeded in HubSpot
        if d["deal_id"] == "d002":
            d["hubspot"] = {"seed": True, "seeded": True, "company_id": "111", "deal_id": "222"}
    (tmp_path / "data" / "deals.json").write_text(json.dumps(deals))
    for name in ("taxonomy.json", "taxonomy_definitions.json"):
        shutil.copy(REPO / "data" / name, tmp_path / "data" / name)
    (tmp_path / "prompts").mkdir()
    shutil.copy(REPO / "prompts" / "transcript_request.md", tmp_path / "prompts" / "transcript_request.md")
    p = Paths(tmp_path)
    p.ensure()
    text = (REPO / "tests" / "fixtures" / "chat_reply_d002.md").read_text().replace("d002", "demo-d02")
    (p.inbox / "demo.md").write_text(text[:text.index("--- EVIDENCE ---")] + "=== END demo-d02 ===\n```\n")
    ingest_inbox(p, settle_seconds=0)
    assert (p.transcripts / "demo-d02.md").exists()
    return p


def test_dry_run_builds_proposals_with_evidence_and_never_calls_slack_or_hubspot(paths):
    payload = blank()
    payload["decision_timeline"] = spec("2027-Q1", "hedged", [(13, "we'd be deciding in Q1 next year")])
    payload["pain_points"] = spec(["rep_ramp_time"], "stated", [(3, "it's taking them five, six months")])
    payload["stage_signal"] = spec("hold", "stated", [(19, "I don't think we'll move faster than that")])
    payload["champion"] = spec("Elena Brooks", "stated", [(11, "this quote is not in the transcript at all")])
    out = pipeline.process_interaction("demo-d02", paths, live=False, settings=SETTINGS, llm_client=FakeLLM(payload))
    assert out["matched"] and out["deal"] == "d002" and out["proposals"] == 3
    assert out["unsupported_fields"] == ["champion"]  # fabricated quote: logged, never proposed
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    rows = proposals_for(conn, out["interaction_id"])
    by = {r["field"]: r for r in rows}
    assert set(by) == {"decision_timeline", "pain_points", "stage_signal"}
    assert by["decision_timeline"]["tentative"] == 1 and by["decision_timeline"]["proposed_value"] == "2027-Q1"
    q = json.loads(by["pain_points"]["evidence"])["quotes"][0]
    assert q["speaker"] == "Elena Brooks" and q["ts"] == "00:34" and q["quote"].startswith("it's taking")
    assert by["stage_signal"]["action"] == "note" and by["stage_signal"]["review_status"] == "pending"
    ctx = json.loads(rows[0]["context"])
    assert ctx["account_name"] == "Harborvine Logistics" and ctx["deal_id"] == "222"
    assert ctx["reviewers"] == ["UREV", "UMGR"]  # dry-run falls back to configured reviewers plus managers
    n_ext = conn.execute("SELECT COUNT(*) FROM extractions WHERE interaction_id=?", (out["interaction_id"],)).fetchone()[0]
    assert n_ext == 9 and conn.execute("SELECT COUNT(*) FROM utterances").fetchone()[0] > 10


def test_no_matching_company_proposes_nothing(paths):
    deals = json.loads(paths.deals.read_text())
    for d in deals["deals"]:
        d["company"]["name"] = "Someone Else Inc"
    paths.deals.write_text(json.dumps(deals))
    out = pipeline.process_interaction("demo-d02", paths, live=False, settings=SETTINGS, llm_client=FakeLLM(blank()))
    assert out["matched"] is False and out["proposals"] == 0


def test_unchanged_crm_values_produce_no_proposals(paths):
    payload = blank()
    payload["pain_points"] = spec(["rep_ramp_time"], "stated", [(3, "it's taking them five, six months")])
    deals = json.loads(paths.deals.read_text())
    for d in deals["deals"]:
        if d["deal_id"] == "d002":
            d["fields"]["pain_points"]["crm_before"] = ["rep_ramp_time"]
    paths.deals.write_text(json.dumps(deals))
    out = pipeline.process_interaction("demo-d02", paths, live=False, settings=SETTINGS, llm_client=FakeLLM(payload))
    assert out["proposals"] == 0


def test_only_demo_calls_reach_the_pipeline(paths):
    from crm.ingest import run_pipeline
    assert "not a demo call" in run_pipeline("d002", paths)


def test_active_rule_reaches_the_prompt_and_versions_are_recorded(paths):
    from crm import rules
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    r = rules.add_candidate(conn, "competitors", "Record a competitor only if the buyer is evaluating it.", "why", [1], "U1")
    v, _ = rules.activate(conn, r, "U1", "demo", validated=False)
    payload = blank()
    payload["pain_points"] = spec(["rep_ramp_time"], "stated", [(3, "it's taking them five, six months")])
    llm = FakeLLM(payload)
    out = pipeline.process_interaction("demo-d02", paths, live=False, settings=SETTINGS, llm_client=llm)
    assert "## Company conventions" in llm.prompts[0] and "only if the buyer is evaluating it" in llm.prompts[0]
    assert out["ruleset_version"] == v
    rows = proposals_for(conn, out["interaction_id"])
    assert rows and {r["ruleset_version_id"] for r in rows} == {v}
    assert {x[0] for x in conn.execute("SELECT ruleset_version_id FROM extractions WHERE interaction_id=?",
                                         (out["interaction_id"],))} == {v}
