"""Review logic for one proposal: card rendering and the click handler. No Slack or HubSpot imports,
so the same code runs from Slack handlers, the dry-run slice, and (M5) the oracle reviewer."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .hubspot import PROPERTY

REJECT_REASONS = ("wrong_value", "wrong_speaker", "stale_or_superseded", "hedged_not_committed",
                  "not_crm_worthy", "evidence_doesnt_support")
FIELD_OF = {v: k for k, v in PROPERTY.items()}


@dataclass
class Outcome:
    status: str  # approved | rejected | stale | not_pending | unauthorized | error
    message: str


def create_proposal(conn: sqlite3.Connection, *, deal_id: str, hubspot_id: str, prop: str, current: str,
                    proposed: str, action: str = "set", tentative: bool = False, evidence: Optional[dict] = None,
                    channel: str = "", context: Optional[dict] = None) -> int:
    cur = conn.execute(
        "INSERT INTO proposals (deal_id, hubspot_object, hubspot_id, property, current_value, proposed_value, "
        "action, tentative, evidence, slack_channel, context) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (deal_id, "deal", hubspot_id, prop, current, proposed, action, int(tentative),
         json.dumps(evidence or {}), channel, json.dumps(context or {})))
    conn.commit()
    return int(cur.lastrowid)


def get_proposal(conn: sqlite3.Connection, pid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()


def record_line(ctx: dict) -> str:
    """'Account Name (account id) | Deal Name (deal id)', with the deal name linking to the HubSpot record."""
    acct = f"*{ctx.get('account_name', 'Unknown account')}* ({ctx.get('account_id', '?')})"
    name, did = ctx.get("deal_name", "Unknown deal"), ctx.get("deal_id", "?")
    deal = f"<{ctx['deal_url']}|{name}>" if ctx.get("deal_url") else name
    return f"{acct}  |  {deal} ({did})"


def card_blocks(row: sqlite3.Row, owner_mention: str = "") -> list:
    """Slack Block Kit card for one proposal. Buttons only while pending; otherwise the final state."""
    ev = json.loads(row["evidence"] or "{}")
    ctx = json.loads(row["context"] or "{}") if "context" in row.keys() else {}
    label = FIELD_OF.get(row["property"], row["property"]).replace("_", " ").capitalize()
    tag = " (tentative)" if row["tentative"] else ""
    cur = row["current_value"] or "(empty)"
    head = f"*{label}*   {cur} → {row['proposed_value']}{tag}"
    quote = ""
    if ev.get("quote"):
        by = f" {ev['speaker']}" if ev.get("speaker") else ""
        quote = f"> \"{ev['quote']}\"\n> —{by}, {ev.get('ts', '')}".rstrip(", ")
    top = f"{owner_mention} {record_line(ctx)}".strip() if ctx else owner_mention
    text = "\n".join(x for x in (top, "1 proposed update", head, quote) if x)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    status = row["review_status"]
    if status == "pending":
        blocks.append({"type": "actions", "block_id": f"proposal_{row['id']}", "elements": [
            {"type": "button", "action_id": "approve", "style": "primary", "value": str(row["id"]),
             "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": "reject", "style": "danger", "value": str(row["id"]),
             "text": {"type": "plain_text", "text": "Reject"}}]})
    else:
        done = {"approved": f"Approved by <@{row['reviewed_by']}>: {row['final_value']}",
                "rejected": f"Rejected by <@{row['reviewed_by']}> ({row['reject_reason']})"}.get(status, status)
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": done}]})
    return blocks


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_current(hs, row: sqlite3.Row) -> str:
    return hs.get_deal(row["hubspot_id"], [row["property"]]).get(row["property"]) or ""


def handle_decision(conn: sqlite3.Connection, hs, pid: int, decision: str, user: str,
                    allowed_users: tuple, reason: str = "") -> Outcome:
    """Approve or reject one proposal. Acts only while pending, only for allowed users, and re-reads the
    CRM value before writing: if it changed since the proposal, the card refreshes instead of writing."""
    row = get_proposal(conn, pid)
    if row is None:
        return Outcome("error", f"proposal {pid} not found")
    if allowed_users and user not in allowed_users:
        return Outcome("unauthorized", "only the owner can review this")
    if row["review_status"] != "pending":  # double-click or Slack retry
        return Outcome("not_pending", f"already {row['review_status']}")
    if decision == "reject":
        conn.execute("UPDATE proposals SET review_status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=? "
                     "WHERE id=? AND review_status='pending'", (reason or "wrong_value", user, _now(), pid))
        conn.commit()
        return Outcome("rejected", "rejected")
    current = _read_current(hs, row)
    if current != (row["current_value"] or ""):
        conn.execute("UPDATE proposals SET current_value=? WHERE id=?", (current, pid))
        conn.commit()
        return Outcome("stale", f"CRM value changed to {current or '(empty)'}; card refreshed, nothing written")
    hs.patch_deal(row["hubspot_id"], {row["property"]: row["proposed_value"]})
    conn.execute("UPDATE proposals SET review_status='approved', final_value=?, reviewed_by=?, reviewed_at=? "
                 "WHERE id=? AND review_status='pending'", (row["proposed_value"], user, _now(), pid))
    conn.commit()
    return Outcome("approved", f"wrote {row['property']}={row['proposed_value']}")
