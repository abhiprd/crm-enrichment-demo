import json
import shutil
from pathlib import Path

import pytest

from crm import evalcmds, evalrun
from crm.config import Settings
from crm.ingest import ingest_inbox
from crm.paths import Paths

REPO = Path(__file__).resolve().parents[1]
FIELDS = ["budget", "decision_timeline", "competitors", "economic_buyer", "champion", "pain_points", "use_case",
          "next_step", "stage_signal"]


class FakeClient:
    """Returns a fixed extraction (the d002 truth) with a per-call flip on stage_signal if asked."""

    def __init__(self, flip_every: int = 0):
        self.n, self.flip_every = 0, flip_every

        class _C:
            pass
        self.chat = _C()
        self.chat.completions = self

    def create(self, **kw):
        self.n += 1
        stage = "advance" if self.flip_every and self.n % self.flip_every == 0 else "hold"
        fields = {f: {"value": None, "status": "not_mentioned", "evidence": []} for f in FIELDS}
        fields["stage_signal"] = {"value": stage, "status": "stated",
                                  "evidence": [{"idx": 3, "quote": "it's taking them five, six months"}]}
        content = json.dumps({"fields": fields})

        class U:
            prompt_tokens, completion_tokens, completion_tokens_details = 100, 50, None

        class M:
            pass
        m = M()
        m.content = content

        class Ch:
            message = m

        class R:
            usage = U
            choices = [Ch]
        return R


@pytest.fixture
def paths(tmp_path) -> Paths:
    (tmp_path / "data").mkdir()
    shutil.copy(REPO / "tests" / "fixtures" / "deals.json", tmp_path / "data" / "deals.json")
    (tmp_path / "prompts").mkdir()
    shutil.copy(REPO / "prompts" / "extractor_v0.md", tmp_path / "prompts" / "extractor_v0.md")
    shutil.copy(REPO / "prompts" / "transcript_request.md", tmp_path / "prompts" / "transcript_request.md")
    shutil.copy(REPO / "data" / "taxonomy.json", tmp_path / "data" / "taxonomy.json")
    p = Paths(tmp_path)
    p.ensure()
    shutil.copy(REPO / "tests" / "fixtures" / "chat_reply_d002.md", p.inbox / "reply.md")
    ingest_inbox(p, settle_seconds=0)
    return p


SETTINGS = Settings(extractor_model="luna", extractor_effort="none", learner_model="sol",
                    prices=(("luna", 0.1, 0.5),))


def test_noise_dry_run_then_run_writes_report(paths):
    plan = evalcmds.noise(SETTINGS, paths, run=False)
    assert plan["dry_run"] and plan["calls"] == 5 and plan["transcripts"] == 1  # d002 is the validation deal
    out = evalcmds.noise(SETTINGS, paths, run=True, client=FakeClient(flip_every=3))
    rep = out["report"]
    assert rep["repeats"] == 5 and rep["calls"] == 5 and rep["call_errors"] == 0
    assert rep["instances_per_run"] == 9 and rep["flipped_instances"] == out["bar_for_paired_tests"]
    assert (paths.root / "results" / "v0_noise.json").exists()


def test_cache_is_keyed_by_prompt_version(paths, tmp_path):
    ids = evalcmds.split_ids(paths, "validation")
    cache = paths.root / "results" / "c.json"
    base = dict(model="luna", effort="none", repeats=1, cache_path=cache, tag="t", client=FakeClient())
    evalrun.run_spec(SETTINGS, paths, ids, template="A {{TRANSCRIPT}}", **base)
    first = len(json.loads(cache.read_text()))
    evalrun.run_spec(SETTINGS, paths, ids, template="A {{TRANSCRIPT}}", **base)  # same prompt: reused
    assert len(json.loads(cache.read_text())) == first
    evalrun.run_spec(SETTINGS, paths, ids, template="B {{TRANSCRIPT}}", **base)  # edited prompt: new entries
    assert len(json.loads(cache.read_text())) == 2 * first


def test_baseline_needs_noise_prompt_and_approval(paths):
    with pytest.raises(SystemExit):
        evalcmds.baseline(SETTINGS, paths, run=False, approved=False)  # no noise file
    evalcmds.noise(SETTINGS, paths, run=True, client=FakeClient())
    with pytest.raises(SystemExit):
        evalcmds.baseline(SETTINGS, paths, run=False, approved=False)  # no baseline prompt yet
    (paths.root / "prompts" / "extractor_baseline.md").write_text("tuned {{TRANSCRIPT}}")
    with pytest.raises(SystemExit):
        evalcmds.baseline(SETTINGS, paths, run=True, approved=False)  # not approved
    out = evalcmds.baseline(SETTINGS, paths, run=True, approved=True, client=FakeClient())
    assert out["paired_vs_v0"]["instances"] == 9 and not out["paired_vs_v0"]["clears_noise_floor"]
    assert (paths.root / "results" / "manual_baseline.json").exists()


def test_learn_errors_only_surface_learn_split(paths):
    # d002 is validation: learn_round has no learn deals here, and learn_errors must never show validation
    out = evalcmds.learn_round(SETTINGS, paths, Path("prompts/extractor_v0.md"), 0, run=True, client=FakeClient())
    assert out["calls"] == 0
    (paths.root / "results" / "learn_runs.json").write_text(json.dumps({
        "x|1": {"deal_id": "d002", "fields": {f: {"correct": False, "score": 0.0} for f in FIELDS},
                "raw": {f: {"value": None, "status": "not_mentioned"} for f in FIELDS}}}))
    assert evalcmds.learn_errors(paths, Path("prompts/extractor_v0.md")) == []


def test_unparseable_reply_is_retried_once(paths):
    from crm import db
    from crm.extractor import extract
    from crm.ingest import parse_transcript

    replies = iter(["not json at all", FakeClient().create().choices[0].message.content])

    class Flaky(FakeClient):
        def create(self, **kw):
            r = super().create(**kw)
            r.choices[0].message.content = next(replies)
            return r

    conn = db.connect(paths.root / "results" / "t.sqlite")
    block = parse_transcript((paths.transcripts / "d002.md").read_text())
    ex = extract(SETTINGS, conn, block, model="luna", effort=None, run_id="t", client=Flaky())
    assert ex.retries == 1 and not ex.errors
    assert [r["purpose"] for r in conn.execute("SELECT purpose FROM llm_calls")] == ["extract", "extract-retry"]
