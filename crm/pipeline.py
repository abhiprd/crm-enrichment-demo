"""The live pipeline for one call: parse, match the deal, read current CRM values, extract, validate quotes,
log, build proposals, and post one review card. Dry-run (default) prints the card and never calls Slack or
HubSpot; the CRM values then come from the seeded deal's stored crm_before (SPEC section 4)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db
from .config import Settings
from .extractor import extract
from .hubspot import PROPERTY, HubSpot, deal_url, props_from_fields
from .ingest import Block, parse_transcript
from .paths import Paths
from .proposals import Proposal, build_proposals
from .review import card_blocks, create_proposal, proposals_for
from .scoring import norm_name
from .validator import apply_validation, validate_fields

DEMO_DEALS = "demo_deals.json"


def _seeded_deals(paths: Paths) -> list:
    """Seeded deals from deals.json and demo_deals.json. Only company, hubspot ids and crm_before are read."""
    out = []
    for path in (paths.deals, paths.data / DEMO_DEALS):
        if path.exists():
            for d in json.loads(path.read_text(encoding="utf-8")).get("deals", []):
                if d.get("hubspot", {}).get("deal_id"):
                    out.append({"deal_id": d["deal_id"], "company": d["company"], "hubspot": d["hubspot"],
                                "crm_before": {f: v.get("crm_before") for f, v in d.get("fields", {}).items()}})
    return out


def match_deal(paths: Paths, block: Block) -> Optional[dict]:
    """The seeded deal whose company name equals an external participant's org."""
    orgs = {norm_name(p.org) for p in block.participants if not p.internal}
    return next((d for d in _seeded_deals(paths) if norm_name(d["company"]["name"]) in orgs), None)


def _clock(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _evidence(block: Block, spec: dict) -> dict:
    """Every cited utterance as {"quotes": [{quote, speaker, ts, idx}]}, in call order, so the card shows all
    the support for a proposal (a competitors change can rest on several lines)."""
    by_idx = {u.idx: u for u in block.utterances}
    quotes, seen = [], set()
    for e in sorted(spec.get("evidence") or [], key=lambda e: e["idx"]):
        u = by_idx.get(e["idx"])
        if u is not None and (u.idx, e["quote"]) not in seen:
            seen.add((u.idx, e["quote"]))
            quotes.append({"quote": e["quote"], "speaker": u.speaker, "ts": _clock(u.seconds), "idx": u.idx})
    return {"quotes": quotes}


def current_values(deal: dict, hs: Optional[HubSpot], live: bool) -> tuple:
    """(field -> CRM text, owner_id). Live reads HubSpot; dry-run uses the stored crm_before."""
    props = list(PROPERTY.values())
    if live:
        got = hs.get_deal(deal["hubspot"]["deal_id"], props + ["hubspot_owner_id"])
        return {f: (got.get(p) or "") for f, p in PROPERTY.items()}, got.get("hubspot_owner_id") or ""
    texts = props_from_fields(deal["crm_before"])
    return {f: texts.get(p, "") for f, p in PROPERTY.items()}, ""


def _slack_user_for(slack, email: str) -> str:
    if not (slack and email):
        return ""
    try:
        return slack.users_lookupByEmail(email=email)["user"]["id"]
    except Exception:  # noqa: BLE001  (missing scope or no such user: fall back to configured reviewers)
        return ""


def process_interaction(deal_id: str, paths: Paths, live: bool = False, settings: Optional[Settings] = None,
                        llm_client=None, hs: Optional[HubSpot] = None, slack=None) -> dict:
    """Process one ingested transcript. Returns a summary dict (also printed in dry-run)."""
    settings = settings or Settings.load()
    block = parse_transcript((paths.transcripts / f"{deal_id}.md").read_text(encoding="utf-8"))
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    interaction_id = f"{deal_id}@{datetime.now().strftime('%Y%m%d%H%M%S')}"
    conn.execute("INSERT INTO interactions (id, source_type, occurred_at, participants, deal_ref) VALUES (?,?,?,?,?)",
                 (interaction_id, "call", block.date,
                  json.dumps([{"name": p.name, "org": p.org, "role": p.role, "internal": p.internal}
                              for p in block.participants]), deal_id))
    conn.executemany("INSERT INTO utterances (interaction_id, idx, speaker, start_ts, text) VALUES (?,?,?,?,?)",
                     [(interaction_id, u.idx, u.speaker, _clock(u.seconds), u.text) for u in block.utterances])
    conn.commit()

    deal = match_deal(paths, block)
    if live and slack is None:
        from slack_sdk import WebClient
        slack = WebClient(token=settings.slack_bot_token)
    if deal is None:
        msg = (f"No seeded deal matches the external company in {deal_id}; nothing proposed. "
               "Seed the company or check the org name.")
        if live:
            slack.chat_postMessage(channel=settings.slack_channel, text=msg)
        return {"interaction_id": interaction_id, "matched": False, "message": msg, "proposals": 0}
    if live and hs is None:
        hs = HubSpot(settings.hubspot_key, live=True)

    current, owner_id = current_values(deal, hs, live)
    ex = extract(settings, conn, block, model=settings.extractor_model, effort=settings.extractor_effort or None,
                 run_id=interaction_id, client=llm_client)
    bad = validate_fields(ex.fields, {u.idx: u.text for u in block.utterances})
    validated = apply_validation(ex.fields, bad)
    ext_ids = {}
    for field, spec in ex.fields.items():  # log what the model said, with unsupported flagged in the evidence
        ev = {"evidence": spec["evidence"], **({"unsupported": bad[field]} if field in bad else {})}
        cur = conn.execute(
            "INSERT INTO extractions (interaction_id, field, value, status, evidence, prompt_version, model, "
            "ruleset_version_id) VALUES (?,?,?,?,?,?,?,NULL)",
            (interaction_id, field, json.dumps(spec["value"]), spec["status"], json.dumps(ev),
             ex.prompt_version, settings.extractor_model))
        ext_ids[field] = cur.lastrowid
    conn.commit()

    owner_slack = ""
    if live:
        owner_slack = _slack_user_for(slack, hs.owner_email(owner_id))
    reviewers = list(dict.fromkeys(([owner_slack] if owner_slack else list(settings.slack_reviewer_ids))
                                   + list(settings.slack_manager_ids)))
    ctx = {"account_name": deal["company"]["name"], "account_id": deal["hubspot"].get("company_id") or "dry-run",
           "deal_name": deal["company"]["name"] + " - Northbeam", "deal_id": deal["hubspot"]["deal_id"],
           "owner_slack_id": owner_slack, "reviewers": reviewers}
    if live:
        acct = hs.account()
        if acct.get("portalId"):
            ctx["deal_url"] = deal_url(acct["portalId"], deal["hubspot"]["deal_id"],
                                       acct.get("uiDomain") or "app.hubspot.com")

    proposals: list = build_proposals(validated, current)
    for p in proposals:
        create_proposal(conn, deal_id=deal["deal_id"], hubspot_id=deal["hubspot"]["deal_id"], prop=p.prop,
                        current=p.current, proposed=p.proposed, action=p.action, tentative=p.tentative,
                        evidence=_evidence(block, validated[p.field]), channel=settings.slack_channel if live else "",
                        context=ctx, interaction_id=interaction_id, extraction_id=ext_ids[p.field], field=p.field)
    summary = {"interaction_id": interaction_id, "matched": True, "deal": deal["deal_id"], "proposals": len(proposals),
               "unsupported_fields": sorted(bad), "retries": ex.retries,
               "fields": [{"field": p.field, "action": p.action, "current": p.current, "proposed": p.proposed,
                           "tentative": p.tentative} for p in proposals]}
    if not proposals:
        if live:
            slack.chat_postMessage(channel=settings.slack_channel,
                                   text=f"{ctx['account_name']}: call processed, no CRM updates proposed.")
        return {**summary, "message": "no updates proposed"}
    if live:
        from .slack_app import post_card
        summary["slack_ts"] = post_card(settings, conn, interaction_id, slack)
    return summary
