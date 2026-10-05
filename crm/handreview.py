"""Hand review (SPEC section 6): the project owner reviews real Slack cards for ~10 calls built with the frozen
final version, and their decisions are compared with what the oracle would have decided on the same proposals.

Cards go to Slack only with --live; clicks are handled against a stub HubSpot, so nothing is written to HubSpot.
Calls come from the validation split (the learn calls shaped the final rules and examples). The oracle's per-proposal
decisions derive from the answer key, so they are stored in results/handreview_oracle_runs.json, which the main
session does not open; `report` prints aggregates only."""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

from . import curve, db, oracle
from .config import Settings
from .deals import load_deals
from .evalcmds import V0_PROMPT, split_ids
from .extractor import extract, load_template, prompt_version
from .ingest import parse_transcript
from .paths import Paths
from .pipeline import _evidence
from .propscore import predicted_proposal
from .review import create_proposal
from .schema import FIELDS
from .scoring import norm_name
from .validator import apply_validation, validate_fields

N_CALLS = 10
SEED = 2026
DB_NAME = "handreview.sqlite"
ORACLE_FILE = "handreview_oracle_runs.json"


class StubHubSpot:
    """Reads a proposal's stored CRM text and swallows writes: hand review never touches HubSpot."""

    def __init__(self, conn_factory):
        self.conn_factory = conn_factory

    def get_deal(self, deal_id: str, properties: list) -> dict:
        conn = self.conn_factory()
        out = {}
        for p in properties:
            row = conn.execute("SELECT current_value FROM proposals WHERE hubspot_id=? AND property=? LIMIT 1",
                               (deal_id, p)).fetchone()
            out[p] = row["current_value"] if row else ""
        return out

    def patch_deal(self, deal_id: str, props: dict) -> dict:
        return {}

    def create_note(self, deal_id: str, text: str) -> dict:
        return {}


def choose_calls(paths: Paths, n: int = N_CALLS) -> list:
    ids = split_ids(paths, "validation")
    random.Random(SEED).shuffle(ids)
    return sorted(ids[:n])


def frozen_template(paths: Paths) -> str:
    tpl = curve.current_template(curve.curve_db(paths), base=load_template(paths.root / V0_PROMPT))
    want = json.loads((paths.root / "results" / "curve.json").read_text())["final"]["prompt_version"]
    if prompt_version(tpl) != want:
        raise SystemExit(f"the final version changed ({prompt_version(tpl)} != {want})")
    return tpl


def build(settings: Settings, paths: Paths, client=None) -> dict:
    """Extract each chosen call with the frozen template, log proposals, and store the oracle's decisions."""
    conn = db.connect(paths.root / "results" / DB_NAME)
    tpl, deals = frozen_template(paths), load_deals(paths).deals
    oracle_rows: dict = {}
    for d in choose_calls(paths):
        if conn.execute("SELECT 1 FROM proposals WHERE deal_id=? LIMIT 1", (d,)).fetchone():
            continue  # already built
        block = parse_transcript((paths.transcripts / f"{d}.md").read_text(encoding="utf-8"))
        key = json.loads((paths.keys / f"{d}.json").read_text(encoding="utf-8"))
        ex = extract(settings, conn, block, model=settings.extractor_model, effort=settings.extractor_effort or None,
                     run_id=f"handreview-{d}", client=client, template=tpl)
        validated = apply_validation(ex.fields, validate_fields(ex.fields, {u.idx: u.text for u in block.utterances}))
        iid = f"{d}@handreview"
        ctx = {"account_name": deals[d]["company"]["name"], "account_id": "hand review",
               "deal_name": f"read data/transcripts/{d}.md first", "deal_id": d, "reviewers": []}
        for f in FIELDS:
            pred = predicted_proposal(f, validated[f], key["fields"][f].get("crm_before"))
            dec = oracle.decide(f, key["fields"][f], pred, deals[d]["cases"])
            if pred is None:
                continue
            pid = create_proposal(conn, deal_id=d, hubspot_id=d, prop=pred.prop, current=pred.current,
                                  proposed=pred.proposed, action=pred.action, tentative=pred.tentative,
                                  evidence=_evidence(block, validated[f]), interaction_id=iid, field=f,
                                  context=ctx)
            oracle_rows[str(pid)] = {"deal": d, "field": f, "kind": dec.kind, "reason": dec.reason, "final": dec.final}
    path = paths.root / "results" / ORACLE_FILE
    old = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**old, **oracle_rows}, indent=1), encoding="utf-8")
    return {"calls": choose_calls(paths), "proposals_new": len(oracle_rows)}


def post(settings: Settings, paths: Paths) -> list:
    """One Slack card per call that has proposals and has not been posted."""
    from .slack_app import post_card
    conn = db.connect(paths.root / "results" / DB_NAME)
    posted = []
    for r in conn.execute("SELECT DISTINCT interaction_id FROM proposals WHERE slack_ts IS NULL ORDER BY interaction_id"):
        posted.append((r["interaction_id"], post_card(settings, conn, r["interaction_id"])))
    return posted


def serve(settings: Settings, paths: Paths) -> None:
    from .slack_app import serve as run
    db_path = paths.root / "results" / DB_NAME
    run(settings, db_path, StubHubSpot(lambda: db.connect(db_path)), None)


def _norm(kind: str, text: Optional[str]) -> str:
    return norm_name(text or "")


def report(paths: Paths) -> dict:
    """Human vs oracle on the same proposals -> results/handreview.json (aggregates only)."""
    conn = db.connect(paths.root / "results" / DB_NAME)
    ora = json.loads((paths.root / "results" / ORACLE_FILE).read_text())
    kind = {"approved": "approve", "edited": "edit", "rejected": "reject"}
    rows = conn.execute("SELECT id, field, review_status, reject_reason, final_value FROM proposals").fetchall()
    done = [r for r in rows if r["review_status"] in kind]
    confusion: Counter = Counter()
    by_field: dict = {}
    same_kind = same_detail = 0
    for r in done:
        o, h = ora[str(r["id"])], kind[r["review_status"]]
        confusion[f"human_{h}__oracle_{o['kind']}"] += 1
        agree = h == o["kind"]
        same_kind += agree
        detail = agree and ((h == "approve") or (h == "reject" and r["reject_reason"] == o["reason"])
                            or (h == "edit" and _norm("", r["final_value"]) == _norm("", o["final"])))
        same_detail += bool(detail)
        by_field.setdefault(r["field"], Counter())["agree" if agree else "differ"] += 1
    n = len(done)
    rate = lambda xs, k: sum(x == k for x in xs) / len(xs) if xs else None  # noqa: E731
    hk, ok = [kind[r["review_status"]] for r in done], [ora[str(r["id"])]["kind"] for r in done]
    out = {"calls": len({r["id"] and ora[str(r["id"])]["deal"] for r in rows}), "proposals": len(rows),
           "reviewed_by_human": n, "still_pending": len(rows) - n,
           "same_decision_kind": same_kind, "same_decision_and_detail": same_detail,
           "confusion": dict(confusion),
           "human_decisions": dict(Counter(hk)), "oracle_decisions_same_proposals": dict(Counter(ok)),
           "human_error_rate_edit_or_reject": (1 - rate(hk, "approve")) if n else None,
           "oracle_error_rate_edit_or_reject": (1 - rate(ok, "approve")) if n else None,
           "by_field": {f: dict(c) for f, c in sorted(by_field.items())},
           "note": "oracle = idealized reviewer from the answer key; reject reason and edit text compared as well "
                   "(same_decision_and_detail). Proposals the human left pending are excluded."}
    (paths.root / "results" / "handreview.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out
