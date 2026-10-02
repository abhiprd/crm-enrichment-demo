import json
import shutil
from pathlib import Path

import pytest

from crm import briefs
from crm.cli import main
from crm.deals import load_deals, validate_deal
from crm.ingest import ingest_inbox, parse_transcript
from crm.paths import Paths

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "chat_reply_d002.md"


@pytest.fixture
def paths(tmp_path) -> Paths:
    (tmp_path / "data").mkdir()
    shutil.copy(REPO / "tests" / "fixtures" / "deals.json", tmp_path / "data" / "deals.json")
    (tmp_path / "prompts").mkdir()
    shutil.copy(REPO / "prompts" / "transcript_request.md", tmp_path / "prompts" / "transcript_request.md")
    p = Paths(tmp_path)
    p.ensure()
    return p


def drop(paths: Paths, text: str, name: str = "reply.md") -> Path:
    f = paths.inbox / name
    f.write_text(text, encoding="utf-8")
    return f


def run(paths: Paths, **kw):
    return ingest_inbox(paths, settle_seconds=0, **kw)


def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_example_deals_are_valid(paths):
    for deal in load_deals(paths).deals.values():
        assert validate_deal(deal) == []


def test_chat_reply_ingests(paths):
    drop(paths, fixture_text())
    [r] = run(paths)
    assert r.ok, r
    assert (paths.transcripts / "d002.md").exists()
    key = json.loads((paths.keys / "d002.json").read_text())
    assert key["split"] == "validation"
    assert key["fields"]["next_step"]["support_idx"] == [16, 17, 18]
    assert key["fields"]["budget"]["support_idx"] == []
    assert key["fields"]["champion"]["truth"]["value"] == "Elena Brooks"
    assert r.moved_to.parent == paths.processed
    assert any("outside DEAL blocks" in w for w in r.file_warnings)
    transcript = (paths.transcripts / "d002.md").read_text()
    assert "EVIDENCE" not in transcript and "===" not in transcript


def test_stored_transcript_round_trips(paths):
    drop(paths, fixture_text())
    run(paths)
    b = parse_transcript((paths.transcripts / "d002.md").read_text())
    assert not b.parse_errors
    assert len(b.utterances) == 22
    assert b.utterances[16].speaker == "Marcus Lee"


@pytest.mark.parametrize("old,new,expect", [
    ('"stage_signal": [19]}', '"stage_signal": [19], "budget": [3]}', "budget is not_mentioned"),
    ('"champion": [11], ', "", "champion is stated"),
    ("doesn't scale.", "doesn't scale, classic hedged_timeline.", "leaks labels"),
    ("[5] [01:12] Elena Brooks", "[5] [01:12] Elena Brookes", "speakers not in Participants"),
    ("[6] [01:15]", "[7] [01:15]", "without gaps"),
    ("[8] [01:58]", "[8] [00:58]", "timestamp goes backwards"),
    ("Call type: demo", "Call type: discovery", "does not match deals.json"),
    ('"next_step": [16, 17, 18]', '"next_step": [16, 99]', "outside 0..21"),
])
def test_rejections(paths, old, new, expect):
    text = fixture_text()
    assert old in text
    drop(paths, text.replace(old, new))
    [r] = run(paths)
    assert not r.ok
    errors = " | ".join(e for b in r.blocks for e in b.errors)
    assert expect in errors
    assert not (paths.transcripts / "d002.md").exists()
    assert r.moved_to.parent == paths.rejected
    assert Path(str(r.moved_to) + ".errors.txt").exists()


def test_existing_transcript_needs_force(paths):
    drop(paths, fixture_text(), "a.md")
    run(paths)
    drop(paths, fixture_text(), "b.md")
    [r] = run(paths)
    assert not r.ok and "already exists" in r.blocks[0].errors[0]
    drop(paths, fixture_text(), "c.md")
    [r] = run(paths, force=True)
    assert r.ok


def test_new_transcript_clears_stale_audit(paths):
    drop(paths, fixture_text(), "a.md")
    run(paths)
    (paths.audit / "d002.json").write_text('{"verdict": "pass"}')
    drop(paths, fixture_text(), "b.md")
    run(paths, force=True)
    assert not (paths.audit / "d002.json").exists()


def test_demo_block_needs_no_deal_or_evidence(paths):
    text = fixture_text().replace("d002", "demo-northwind")
    text = text.split("--- EVIDENCE ---")[0] + "=== END demo-northwind ===\n"
    text = text.replace("Date: 2026-09-18", "Date: 2026-10-01")
    drop(paths, text)
    [r] = run(paths)
    assert r.ok, r
    assert (paths.transcripts / "demo-northwind.md").exists()
    assert not (paths.keys / "demo-northwind.json").exists()


def test_unknown_deal_rejected(paths):
    drop(paths, fixture_text().replace("d002", "d999"))
    [r] = run(paths)
    assert "not in data/deals.json" in r.blocks[0].errors[0]


def test_unclosed_block_rejected(paths):
    drop(paths, fixture_text().replace("=== END d002 ===", ""))
    [r] = run(paths)
    assert not r.ok and "never closed" in r.file_errors[0]


def test_empty_file_rejected(paths):
    drop(paths, "Sorry, I can't help with that.")
    [r] = run(paths)
    assert r.file_errors == ["no DEAL blocks found"]


def test_one_bad_block_keeps_the_good_one(paths):
    good = fixture_text()
    bad = good.replace("d002", "d999")
    drop(paths, good + "\n" + bad)
    [r] = run(paths)
    assert not r.ok
    assert [b.ok for b in r.blocks] == [True, False]
    assert (paths.transcripts / "d002.md").exists()


def test_request_excludes_answer_side_data(paths):
    drop(paths, fixture_text())
    run(paths)
    dealset = load_deals(paths)
    deals = briefs.select_deals(paths, dealset, n=5)
    assert [d["deal_id"] for d in deals] == ["d001", "d003"]
    text = briefs.render_request(paths, dealset, deals)
    for banned in ("crm_before", "expected_proposal", "crm_relation", '"split"', "hubspot"):
        assert banned not in text
    assert "d001" in text and "Kestrel Foods" in text and "{{" not in text


def test_cli_status_and_request(paths, capsys):
    assert main(["--root", str(paths.root), "status"]) == 0
    assert "validation" in capsys.readouterr().out
    assert main(["--root", str(paths.root), "request", "--split", "test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["deals"] == ["d003"]
    assert (paths.root / out["request"]).exists()
