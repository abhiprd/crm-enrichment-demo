"""Rule learner: a rejected proposal becomes a candidate rule written by the learner model (Sol).

Code, not the model, enforces the limits: one field, short, no names from the call, no duplicates."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import rules
from .config import Settings
from .extractor import _loads
from .llm import complete
from .review import REJECT_REASONS, get_proposal
from .schema import FIELDS

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "rule_learner.md"
MAX_RULE_CHARS = 300
REASON_MEANING = {
    "wrong_value": "the value is wrong", "wrong_speaker": "it was attributed to the wrong person",
    "stale_or_superseded": "the value was changed later in the call or is out of date",
    "hedged_not_committed": "the speaker did not actually commit to it",
    "not_crm_worthy": "true, but not something the company records in the CRM",
    "evidence_doesnt_support": "the quoted evidence does not support the value",
}


@dataclass
class LearnResult:
    status: str  # created | skipped
    reason: str = ""
    rule_id: Optional[int] = None
    rule_text: str = ""


def _words(text: str) -> set:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def name_leaks(text: str, participants: list) -> list:
    """Names of the call's people or companies that appear in the rule text (it must be general)."""
    low, hits = text.lower(), []
    for p in participants:
        for name in (p.get("name", ""), p.get("org", "")):
            if name and name.lower() in low:
                hits.append(name)
        for tok in re.findall(r"[A-Za-z]{4,}", p.get("name", "")):
            if re.search(rf"\b{re.escape(tok.lower())}\b", low):
                hits.append(tok)
    return sorted(set(hits))


def is_duplicate(text: str, existing: list) -> bool:
    a = _words(text)
    for r in existing:
        b = _words(r["rule_text"])
        if a and b and len(a & b) / len(a | b) >= 0.8:
            return True
    return False


def check_rule(text: Optional[str], participants: list, existing: list) -> str:
    """'' if the rule is acceptable, else why not."""
    if not text or not text.strip():
        return "no rule proposed"
    if len(text) > MAX_RULE_CHARS:
        return f"rule is {len(text)} characters (limit {MAX_RULE_CHARS})"
    leaks = name_leaks(text, participants)
    if leaks:
        return f"rule names something from the call ({', '.join(leaks)}); it must be general"
    if is_duplicate(text, existing):
        return "duplicates a rule already in force"
    return ""


def build_prompt(row: sqlite3.Row, extraction: Optional[sqlite3.Row], existing: list, template: Optional[str] = None) -> str:
    tpl = template if template is not None else PROMPT_PATH.read_text(encoding="utf-8")
    ev = json.loads(row["evidence"] or "{}")
    quotes = ev.get("quotes") or ([ev] if ev.get("quote") else [])
    quote_lines = "\n".join(f'  - "{q["quote"]}" ({q.get("speaker", "?")}, {q.get("ts", "")})' for q in quotes) or "  (none)"
    value = extraction["value"] if extraction else row["proposed_value"]
    status = extraction["status"] if extraction else "stated"
    existing_lines = "\n".join(f"- {r['rule_text']}" for r in existing) or "(none)"
    subs = {"{{FIELD}}": row["field"], "{{STATUS}}": status, "{{VALUE}}": str(value),
            "{{CURRENT}}": row["current_value"] or "(empty)", "{{REASON}}": row["reject_reason"] or "",
            "{{REASON_MEANING}}": REASON_MEANING.get(row["reject_reason"] or "", ""),
            "{{NOTE}}": row["note"] or "(none)", "{{QUOTES}}": quote_lines, "{{EXISTING}}": existing_lines}
    for k, v in subs.items():
        tpl = tpl.replace(k, v)
    return tpl


def learn_from_reject(settings: Settings, conn: sqlite3.Connection, pid: int, client=None) -> LearnResult:
    """Draft a candidate rule from a rejected proposal. Creates a candidate row only if every check passes."""
    row = get_proposal(conn, pid)
    if row is None or row["review_status"] != "rejected":
        return LearnResult("skipped", "proposal is not a rejected one")
    if row["field"] not in FIELDS or row["reject_reason"] not in REJECT_REASONS:
        return LearnResult("skipped", "proposal has no learnable field or reason")
    extraction = conn.execute("SELECT value, status FROM extractions WHERE id=?", (row["extraction_id"],)).fetchone() \
        if row["extraction_id"] else None
    existing = [r for r in rules.active_rules(conn) if r["field"] == row["field"]]
    res = complete(settings, conn, build_prompt(row, extraction, existing), model=settings.learner_model,
                   effort=settings.learner_effort or None, run_id=f"learn-{pid}", purpose="rule-learner", client=client)
    try:
        draft = _loads(res.text)
    except (json.JSONDecodeError, AttributeError):
        return LearnResult("skipped", "the learner returned unparseable output")
    text = (draft.get("rule_text") or "").strip() if isinstance(draft, dict) else ""
    inter = conn.execute("SELECT participants FROM interactions WHERE id=?", (row["interaction_id"],)).fetchone()
    participants = json.loads(inter["participants"]) if inter and inter["participants"] else []
    problem = check_rule(text, participants, existing)
    if problem:
        why = problem if text else (draft.get("rationale") if isinstance(draft, dict) else "") or problem
        return LearnResult("skipped", why)
    rid = rules.add_candidate(conn, row["field"], text, draft.get("rationale", ""), [pid], row["reviewed_by"] or "")
    return LearnResult("created", "", rid, text)
