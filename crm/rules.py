"""Rule store: plain-English extractor instructions learned from rejected proposals, kept in append-only
ruleset versions. A version row is never changed; activating, retiring, or reverting adds a new one."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional

MAX_ACTIVE_PER_FIELD = 5


def _rules_hash(rows: list) -> str:
    text = "\n".join(f"{r['field']}:{r['rule_text']}" for r in rows)
    return "rs-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _insert_version(conn: sqlite3.Connection, parent: Optional[int], active_ids: list, change_type: str,
                    rule_id: Optional[int], reason: str, author: str) -> int:
    rows = [conn.execute("SELECT field, rule_text FROM rules WHERE rule_id=?", (i,)).fetchone() for i in active_ids]
    cur = conn.execute(
        "INSERT INTO ruleset_versions (parent_version_id, active_rule_ids, prompt_template_hash, change_type, "
        "changed_rule_id, reason, author) VALUES (?,?,?,?,?,?,?)",
        (parent, json.dumps(sorted(active_ids)), _rules_hash(rows), change_type, rule_id, reason, author))
    conn.commit()
    return int(cur.lastrowid)


def current_version(conn: sqlite3.Connection) -> sqlite3.Row:
    """The latest ruleset version; the empty starting version is created on first use."""
    row = conn.execute("SELECT * FROM ruleset_versions ORDER BY version_id DESC LIMIT 1").fetchone()
    if row is None:
        _insert_version(conn, None, [], "init", None, "empty starting ruleset", "system")
        row = conn.execute("SELECT * FROM ruleset_versions ORDER BY version_id DESC LIMIT 1").fetchone()
    return row


def get_version(conn: sqlite3.Connection, version_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM ruleset_versions WHERE version_id=?", (version_id,)).fetchone()


def active_rules(conn: sqlite3.Connection, version_id: Optional[int] = None) -> list:
    """Rules active in a version (default: the current one), ordered by field then id."""
    v = get_version(conn, version_id) if version_id else current_version(conn)
    ids = json.loads(v["active_rule_ids"])
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return conn.execute(f"SELECT * FROM rules WHERE rule_id IN ({q}) ORDER BY field, rule_id", ids).fetchall()


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM rules WHERE rule_id=?", (rule_id,)).fetchone()


def add_candidate(conn: sqlite3.Connection, field: str, rule_text: str, rationale: str, source_proposal_ids: list,
                  created_by: str) -> int:
    cur = conn.execute("INSERT INTO rules (field, rule_text, rationale, source_proposal_ids, created_by, status) "
                       "VALUES (?,?,?,?,?, 'candidate')",
                       (field, rule_text, rationale, json.dumps(source_proposal_ids), created_by))
    conn.commit()
    return int(cur.lastrowid)


def activate(conn: sqlite3.Connection, rule_id: int, author: str, reason: str, validated: Optional[bool] = None) -> tuple:
    """Make a candidate active in a new version. Returns (version_id, "") or (None, why-not).
    A field holds at most MAX_ACTIVE_PER_FIELD active rules; a further rule stays a candidate."""
    rule = get_rule(conn, rule_id)
    if rule is None or rule["status"] != "candidate":
        return None, "only a candidate rule can be activated"
    cur = current_version(conn)
    ids = json.loads(cur["active_rule_ids"])
    in_field = [r for r in active_rules(conn) if r["field"] == rule["field"]]
    if len(in_field) >= MAX_ACTIVE_PER_FIELD:
        return None, f"{rule['field']} already has {MAX_ACTIVE_PER_FIELD} active rules; retire one first"
    # validated: 1 = passed the gate; NULL = not validated yet (demo mode); 0 is reserved for a failed gate
    conn.execute("UPDATE rules SET status='active', validated=? WHERE rule_id=?", (1 if validated else None, rule_id))
    return _insert_version(conn, cur["version_id"], ids + [rule_id], "add", rule_id, reason, author), ""


def retire(conn: sqlite3.Connection, rule_id: int, author: str, reason: str) -> Optional[int]:
    cur = current_version(conn)
    ids = json.loads(cur["active_rule_ids"])
    if rule_id not in ids:
        return None
    conn.execute("UPDATE rules SET status='retired' WHERE rule_id=?", (rule_id,))
    return _insert_version(conn, cur["version_id"], [i for i in ids if i != rule_id], "retire", rule_id, reason, author)


def revert(conn: sqlite3.Connection, to_version_id: int, author: str, reason: str) -> Optional[int]:
    """A revert is a new version that copies an earlier version's active set. Nothing is rewritten."""
    target = get_version(conn, to_version_id)
    if target is None:
        return None
    cur = current_version(conn)
    want = set(json.loads(target["active_rule_ids"]))
    for rid in json.loads(cur["active_rule_ids"]):
        if rid not in want:
            conn.execute("UPDATE rules SET status='reverted' WHERE rule_id=?", (rid,))
    for rid in want:
        conn.execute("UPDATE rules SET status='active' WHERE rule_id=?", (rid,))
    return _insert_version(conn, cur["version_id"], sorted(want), "revert", None,
                           f"revert to version {to_version_id}: {reason}", author)


def record_validation(conn: sqlite3.Connection, rule_id: int, validated: bool, gate: dict,
                      score_before: Optional[float] = None, score_after: Optional[float] = None) -> None:
    """Store the gate verdict on the rule and the validation scores on the version that added it."""
    conn.execute("UPDATE rules SET validated=?, gate_result=? WHERE rule_id=?",
                 (int(validated), json.dumps(gate), rule_id))
    if score_before is not None or score_after is not None:
        # scores belong to the version row of the change; set once, on a row not yet scored
        conn.execute("UPDATE ruleset_versions SET validation_score_before=?, validation_score_after=? "
                     "WHERE changed_rule_id=? AND change_type='add' AND validation_score_before IS NULL",
                     (score_before, score_after, rule_id))
    conn.commit()


def examples_for(conn: sqlite3.Connection, field: str, n: int = 3) -> list:
    """The n most recent reviewer edits on a field: what was proposed, what the reviewer wrote, and the quote."""
    out = []
    for r in conn.execute("SELECT proposed_value, final_value, evidence FROM proposals WHERE field=? AND "
                          "review_status='edited' ORDER BY reviewed_at DESC, id DESC LIMIT ?", (field, n)):
        ev = json.loads(r["evidence"] or "{}")
        quotes = ev.get("quotes") or ([ev] if ev.get("quote") else [])
        out.append({"proposed": r["proposed_value"], "final": r["final_value"],
                    "quote": quotes[0]["quote"] if quotes else ""})
    return out
