"""Oracle reviewer (SPEC section 6): an idealized reviewer that plays the Slack review from the answer key.

It goes through the same `review.handle_decision` code path as a Slack click, against a stub HubSpot. Rules:
  - the proposal equals `expected_proposal`: approve
  - nothing should have been proposed (house rule, negation, no change): reject, reason from the trap or house rule
  - a proposal was due but the value is wrong or in the wrong form: edit, supplying the expected final text;
    a competitor list that only adds a not-CRM-worthy vendor to the right ones is a reject (not_crm_worthy)
  - a proposal that should have been made but wasn't gives the oracle nothing to click, so it is never reviewed
It never rubber-stamps a wrong value, never rejects a correct one, and gives no free-text note (only the reason
code), so the learner must work from the reason, the quote, and the extraction. Keys are read by code only."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from .extractor import Extraction, extract
from .hubspot import props_from_fields
from .ingest import Block
from .paths import Paths
from .pipeline import _evidence
from .propscore import _new_pairs, expected_final_text, predicted_proposal, proposal_correct
from .proposals import Proposal
from .review import create_proposal, handle_decision
from .schema import FIELDS, HOUSE_RULES
from .scoring import pairs
from .validator import apply_validation, validate_fields

USER = "oracle"


@dataclass
class Decision:
    kind: str  # approve | edit | reject
    reason: str = ""
    final: Optional[str] = None


class StubHubSpot:
    """Just enough HubSpot for handle_decision: reads the stored CRM text, records writes."""

    def __init__(self, crm_before: dict):
        self.props = props_from_fields(crm_before)
        self.writes: list = []

    def get_deal(self, deal_id: str, properties: list) -> dict:
        return {p: self.props.get(p, "") for p in properties}

    def patch_deal(self, deal_id: str, props: dict) -> dict:
        self.props.update(props)
        self.writes.append(("patch", props))
        return {}

    def create_note(self, deal_id: str, text: str) -> dict:
        self.writes.append(("note", text))
        return {}


def reject_reason(field: str, truth_status: str, cases: list) -> str:
    """The reason code a careful reviewer would pick, derived from the case labels and the truth status."""
    ids = {c["id"] for c in cases if c["field"] == field}
    if ids & set(HOUSE_RULES):
        return "not_crm_worthy"
    if "speaker_attribution" in ids:
        return "wrong_speaker"
    return {"hedged": "hedged_not_committed", "superseded": "stale_or_superseded", "negated": "evidence_doesnt_support",
            "not_mentioned": "evidence_doesnt_support"}.get(truth_status, "wrong_value")


def decide(field: str, key_field: dict, pred: Optional[Proposal], cases: list) -> Optional[Decision]:
    """The oracle's decision on one proposal, or None when there is no proposal to review."""
    if pred is None:
        return None
    expected = key_field.get("expected_proposal")
    if proposal_correct(field, expected, pred):
        return Decision("approve")
    house = bool({c["id"] for c in cases if c["field"] == field} & set(HOUSE_RULES))
    if expected is None:
        return Decision("reject", reject_reason(field, key_field["truth"]["status"], cases))
    if house and field == "competitors" and _new_pairs(pred) > pairs(expected["value"]):
        return Decision("reject", "not_crm_worthy")
    return Decision("edit", final=expected_final_text(field, expected, key_field.get("crm_before")))


def review_extraction(conn: sqlite3.Connection, paths: Paths, deal_id: str, block: Block, ex: Extraction, key: dict,
                      cases: list, ruleset_version: int, model: str) -> dict:
    """Log the interaction, extractions and proposals for one call, then let the oracle decide on each proposal.
    Returns {"decisions": [{field, kind, reason, proposal_id}], "interaction_id"}."""
    interaction_id = f"{deal_id}@curve{ruleset_version}"
    conn.execute("INSERT OR REPLACE INTO interactions (id, source_type, occurred_at, participants, deal_ref) "
                 "VALUES (?,?,?,?,?)",
                 (interaction_id, "call", block.date,
                  json.dumps([{"name": p.name, "org": p.org, "role": p.role, "internal": p.internal}
                              for p in block.participants]), deal_id))
    bad = validate_fields(ex.fields, {u.idx: u.text for u in block.utterances})
    validated = apply_validation(ex.fields, bad)
    ext_ids = {}
    for f, spec in ex.fields.items():
        cur = conn.execute(
            "INSERT INTO extractions (interaction_id, field, value, status, evidence, prompt_version, model, "
            "ruleset_version_id) VALUES (?,?,?,?,?,?,?,?)",
            (interaction_id, f, json.dumps(spec["value"]), spec["status"], json.dumps({"evidence": spec["evidence"]}),
             ex.prompt_version, model, ruleset_version))
        ext_ids[f] = cur.lastrowid
    conn.commit()
    crm_before = {f: key["fields"][f].get("crm_before") for f in FIELDS}
    hs, out = StubHubSpot(crm_before), []
    for f in FIELDS:
        pred = predicted_proposal(f, validated[f], crm_before[f])
        d = decide(f, key["fields"][f], pred, cases)
        if d is None:
            continue
        pid = create_proposal(conn, deal_id=deal_id, hubspot_id=deal_id, prop=pred.prop, current=pred.current,
                              proposed=pred.proposed, action=pred.action, tentative=pred.tentative,
                              evidence=_evidence(block, validated[f]), interaction_id=interaction_id,
                              extraction_id=ext_ids[f], field=f, ruleset_version_id=ruleset_version,
                              context={"reviewers": [USER]})
        res = handle_decision(conn, hs, pid, d.kind, USER, allowed_users=(USER,), reason=d.reason, final_value=d.final)
        out.append({"field": f, "kind": d.kind, "reason": d.reason, "proposal_id": pid, "outcome": res.status})
    return {"interaction_id": interaction_id, "decisions": out}
