"""Review logic for proposals: card rendering, modals, authorization, and the click handler. No Slack or
HubSpot imports, so the same code runs from Slack handlers, the dry-run, and (M5) the oracle reviewer."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .hubspot import PROPERTY
from .proposals import build_proposals

REJECT_REASONS = ("wrong_value", "wrong_speaker", "stale_or_superseded", "hedged_not_committed",
                  "not_crm_worthy", "evidence_doesnt_support")
FIELD_OF = {v: k for k, v in PROPERTY.items()}
FIELD_OF["note"] = "stage_signal"
MAX_QUOTES = 4


@dataclass
class Outcome:
    status: str  # approved | edited | rejected | stale | resolved | not_pending | unauthorized | error
    message: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_proposal(conn: sqlite3.Connection, *, deal_id: str, hubspot_id: str, prop: str, current: str,
                    proposed: str, action: str = "set", tentative: bool = False, evidence: Optional[dict] = None,
                    channel: str = "", context: Optional[dict] = None, interaction_id: str = "",
                    extraction_id: Optional[int] = None, field: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO proposals (extraction_id, deal_id, hubspot_object, hubspot_id, property, current_value, "
        "proposed_value, action, tentative, evidence, slack_channel, context, interaction_id, field) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (extraction_id, deal_id, "deal", hubspot_id, prop, current, proposed, action, int(tentative),
         json.dumps(evidence or {}), channel, json.dumps(context or {}), interaction_id, field or FIELD_OF.get(prop, "")))
    conn.commit()
    return int(cur.lastrowid)


def get_proposal(conn: sqlite3.Connection, pid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()


def proposals_for(conn: sqlite3.Connection, interaction_id: str) -> list:
    return conn.execute("SELECT * FROM proposals WHERE interaction_id = ? ORDER BY id", (interaction_id,)).fetchall()


def record_line(ctx: dict) -> str:
    """'Account Name (account id) | Deal Name (deal id)', with the deal name linking to the HubSpot record."""
    acct = f"*{ctx.get('account_name', 'Unknown account')}* ({ctx.get('account_id', '?')})"
    name, did = ctx.get("deal_name", "Unknown deal"), ctx.get("deal_id", "?")
    deal = f"<{ctx['deal_url']}|{name}>" if ctx.get("deal_url") else name
    return f"{acct}  |  {deal} ({did})"


def _label(row: sqlite3.Row) -> str:
    return (row["field"] or FIELD_OF.get(row["property"], row["property"])).replace("_", " ").capitalize()


def _proposal_text(row: sqlite3.Row) -> str:
    ev = json.loads(row["evidence"] or "{}")
    tag = " (tentative)" if row["tentative"] else ""
    if row["action"] == "note":
        head = f"*{_label(row)}*   add note: {row['proposed_value']}{tag}"
    elif row["action"] == "clear":
        head = f"*{_label(row)}*   {row['current_value'] or '(empty)'} → (clear){tag}"
    else:
        head = f"*{_label(row)}*   {row['current_value'] or '(empty)'} → {row['proposed_value']}{tag}"
    quotes = ev.get("quotes") or ([ev] if ev.get("quote") else [])  # older rows hold one quote
    lines = []
    for q in quotes[:MAX_QUOTES]:
        by = f" {q['speaker']}" if q.get("speaker") else ""
        lines.append(f"> \"{q['quote'][:400]}\"\n> —{by}, {q.get('ts', '')}".rstrip(", "))
    if len(quotes) > MAX_QUOTES:
        lines.append(f"> (+{len(quotes) - MAX_QUOTES} more cited lines)")
    quote = "\n".join(lines)
    return "\n".join(x for x in (head, quote) if x)


def _final_line(row: sqlite3.Row) -> str:
    who = f"<@{row['reviewed_by']}>" if row["reviewed_by"] and row["reviewed_by"] != "system" else "the system"
    status = row["review_status"]
    if status == "approved":
        return f"Approved by {who}: {row['final_value'] or '(cleared)'}"
    if status == "edited":
        return f"Edited by {who}: {row['final_value'] or '(cleared)'}"
    if status == "rejected":
        return f"Rejected by {who} ({row['reject_reason']})"
    return status


def card_blocks(rows, owner_mention: str = "") -> list:
    """One card per interaction: a header, then one section per proposal with its own buttons.
    `rows` may be a single row. There is deliberately no approve-all control."""
    rows = [rows] if isinstance(rows, sqlite3.Row) else list(rows)
    ctx = json.loads(rows[0]["context"] or "{}") if rows and "context" in rows[0].keys() else {}
    n = len(rows)
    if not owner_mention and ctx.get("owner_slack_id"):
        owner_mention = f"<@{ctx['owner_slack_id']}>"
    top = " ".join(x for x in (owner_mention, record_line(ctx) if ctx else "") if x)
    header = "\n".join(x for x in (top, f"{n} proposed update{'s' if n != 1 else ''}") if x)
    blocks: list = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}]
    for row in rows:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _proposal_text(row)[:2900]}})
        if row["review_status"] == "pending":
            pid = str(row["id"])
            blocks.append({"type": "actions", "block_id": f"proposal_{pid}", "elements": [
                {"type": "button", "action_id": "approve", "style": "primary", "value": pid,
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": "edit", "value": pid, "text": {"type": "plain_text", "text": "Edit"}},
                {"type": "button", "action_id": "reject", "style": "danger", "value": pid,
                 "text": {"type": "plain_text", "text": "Reject"}}]})
        else:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _final_line(row)}]})
    return blocks


# ---- modals ---------------------------------------------------------------------------------------

def edit_modal(row: sqlite3.Row, channel: str, ts: str) -> dict:
    meta = json.dumps({"pid": row["id"], "channel": channel, "ts": ts})
    return {"type": "modal", "callback_id": "edit_submit", "private_metadata": meta,
            "title": {"type": "plain_text", "text": f"Edit {_label(row)}"[:24]},
            "submit": {"type": "plain_text", "text": "Save and write"},
            "blocks": [{"type": "input", "block_id": "value", "label": {"type": "plain_text", "text": "Final value"},
                        "element": {"type": "plain_text_input", "action_id": "text", "multiline": row["action"] == "note",
                                    "initial_value": row["proposed_value"] or ""}, "optional": False}]}


def reject_modal(row: sqlite3.Row, channel: str, ts: str) -> dict:
    meta = json.dumps({"pid": row["id"], "channel": channel, "ts": ts})
    options = [{"text": {"type": "plain_text", "text": r}, "value": r} for r in REJECT_REASONS]
    return {"type": "modal", "callback_id": "reject_submit", "private_metadata": meta,
            "title": {"type": "plain_text", "text": f"Reject {_label(row)}"[:24]},
            "submit": {"type": "plain_text", "text": "Reject"},
            "blocks": [
                {"type": "input", "block_id": "reason", "label": {"type": "plain_text", "text": "Reason"},
                 "element": {"type": "static_select", "action_id": "choice", "options": options,
                             "initial_option": options[0]}},
                {"type": "input", "block_id": "note", "optional": True, "label": {"type": "plain_text", "text": "Note"},
                 "element": {"type": "plain_text_input", "action_id": "text"}}]}


def modal_values(view: dict) -> dict:
    """Pull submitted values from a view payload: {pid, channel, ts, text, reason, note}."""
    meta = json.loads(view.get("private_metadata") or "{}")
    vals = view.get("state", {}).get("values", {})
    text = (vals.get("value", {}).get("text", {}) or {}).get("value")
    choice = (vals.get("reason", {}).get("choice", {}) or {}).get("selected_option") or {}
    note = (vals.get("note", {}).get("text", {}) or {}).get("value") or ""
    return {**meta, "text": text, "reason": choice.get("value", ""), "note": note}


# ---- authorization, stale handling, writeback -----------------------------------------------------

def reviewers_for(row: sqlite3.Row) -> tuple:
    """The Slack users allowed to act on this proposal: the mapped record owner plus managers."""
    return tuple(json.loads(row["context"] or "{}").get("reviewers", []))


def _read_current(hs, row: sqlite3.Row) -> str:
    return hs.get_deal(row["hubspot_id"], [row["property"]]).get(row["property"]) or ""


def _equivalent(a: str, b: str) -> bool:
    """HubSpot stores amounts as '60000.0' or '60000'; treat numeric-equal text as equal."""
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def refresh_stale(conn: sqlite3.Connection, pid: int, current: str) -> Outcome:
    """The CRM value changed since the proposal. Re-diff from the stored extraction: update the card, or
    resolve the proposal if the CRM already holds what was proposed. Nothing is written to HubSpot."""
    row = get_proposal(conn, pid)
    rebuilt = None
    if row["extraction_id"]:
        ex = conn.execute("SELECT value, status FROM extractions WHERE id = ?", (row["extraction_id"],)).fetchone()
        if ex is not None:
            spec = {"value": json.loads(ex["value"]) if ex["value"] else None, "status": ex["status"]}
            built = build_proposals({row["field"]: spec}, {row["field"]: current})
            rebuilt = built[0] if built else None
    if row["extraction_id"] and rebuilt is None:
        conn.execute("UPDATE proposals SET current_value=?, review_status='rejected', reject_reason="
                     "'stale_or_superseded', reviewed_by='system', reviewed_at=? WHERE id=?", (current, _now(), pid))
        conn.commit()
        return Outcome("resolved", "the CRM already holds the proposed value; proposal closed")
    if rebuilt is not None:
        conn.execute("UPDATE proposals SET current_value=?, proposed_value=?, action=? WHERE id=?",
                     (current, rebuilt.proposed, rebuilt.action, pid))
    else:
        conn.execute("UPDATE proposals SET current_value=? WHERE id=?", (current, pid))
    conn.commit()
    return Outcome("stale", f"CRM value changed to {current or '(empty)'}; card refreshed, nothing written")


def _write(hs, row: sqlite3.Row, text: str) -> str:
    if row["action"] == "note":
        hs.create_note(row["hubspot_id"], text)
        return f"added note on the deal: {text}"
    value = "" if row["action"] == "clear" else text
    hs.patch_deal(row["hubspot_id"], {row["property"]: value})
    return f"wrote {row['property']}={value or '(cleared)'}"


def handle_decision(conn: sqlite3.Connection, hs, pid: int, decision: str, user: str,
                    allowed_users: Optional[tuple] = None, reason: str = "", note: str = "",
                    final_value: Optional[str] = None) -> Outcome:
    """approve | edit | reject one proposal. Acts only while pending and only for allowed users. Before a
    write it re-reads the CRM value: if that changed since the proposal, the card refreshes and nothing
    is written. Edit writes `final_value`."""
    row = get_proposal(conn, pid)
    if row is None:
        return Outcome("error", f"proposal {pid} not found")
    allowed = allowed_users if allowed_users is not None else reviewers_for(row)
    if allowed and user not in allowed:
        return Outcome("unauthorized", "only the owner can review this")
    if row["review_status"] != "pending":  # double-click or Slack retry
        return Outcome("not_pending", f"already {row['review_status']}")
    if decision == "reject":
        conn.execute("UPDATE proposals SET review_status='rejected', reject_reason=?, note=?, reviewed_by=?, "
                     "reviewed_at=? WHERE id=? AND review_status='pending'",
                     (reason or "wrong_value", note, user, _now(), pid))
        conn.commit()
        return Outcome("rejected", "rejected")
    if decision == "edit" and (final_value is None or not final_value.strip() and row["action"] != "clear"):
        return Outcome("error", "an edited value cannot be empty")
    if row["action"] != "note":
        current = _read_current(hs, row)
        if not _equivalent(current, row["current_value"] or ""):
            return refresh_stale(conn, pid, current)
    text = final_value if decision == "edit" else row["proposed_value"]
    done = _write(hs, row, text or "")
    status = "edited" if decision == "edit" else "approved"
    conn.execute("UPDATE proposals SET review_status=?, final_value=?, reviewed_by=?, reviewed_at=? "
                 "WHERE id=? AND review_status='pending'", (status, text, user, _now(), pid))
    conn.commit()
    return Outcome(status, done)
