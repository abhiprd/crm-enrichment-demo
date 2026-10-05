"""Proposal-level scoring (PLAN decision D1): did the extraction lead to the proposal that should land in the CRM
after house rules? The key's `expected_proposal` is the target; `truth` scoring (crm/scoring.py) is unchanged.

An instance (deal, field) is correct when the proposal the pipeline would build from the validated extraction
agrees with `expected_proposal`: both absent, or the same action and the same value. Pure functions; keys are
read by callers in code only."""

from __future__ import annotations

from typing import Any, Optional

from .hubspot import fmt_value
from .proposals import Proposal, build_proposals, parse_competitors, parse_labels
from .schema import FIELDS
from .scoring import norm_name, pairs, quarter_of, to_amount


def predicted_proposal(field: str, spec: dict, crm_before: Any) -> Optional[Proposal]:
    """The proposal the pipeline would build for one validated field against the CRM value before the call."""
    built = build_proposals({field: spec}, {field: fmt_value(field, crm_before)})
    return built[0] if built else None


def _new_pairs(pred: Proposal) -> set:
    return pairs(parse_competitors(pred.proposed)) - pairs(parse_competitors(pred.current))


def _new_labels(pred: Proposal) -> set:
    return set(parse_labels(pred.proposed)) - set(parse_labels(pred.current))


def _value_matches(field: str, expected_value: Any, pred: Proposal) -> bool:
    if field == "competitors":
        return _new_pairs(pred) == pairs(expected_value)
    if field in ("pain_points", "use_case"):
        return _new_labels(pred) == set(expected_value)
    if field == "budget":
        return to_amount(pred.proposed) is not None and to_amount(pred.proposed) == to_amount(fmt_value(field, expected_value))
    if field == "decision_timeline":
        return str(pred.proposed).strip().upper() == str(expected_value).strip().upper() \
            and quarter_of(pred.proposed) is not None
    if field == "stage_signal":
        return norm_name(pred.proposed) == norm_name(f"Stage signal from call: {expected_value}")
    return norm_name(pred.proposed) == norm_name(fmt_value(field, expected_value))


def proposal_correct(field: str, expected: Optional[dict], pred: Optional[Proposal]) -> bool:
    """expected: the key's expected_proposal ({action, value, tentative?}) or None for no proposal."""
    if expected is None or pred is None:
        return expected is None and pred is None
    if pred.action != expected["action"]:
        return False
    if bool(expected.get("tentative")) != bool(pred.tentative):
        return False
    return _value_matches(field, expected["value"], pred)


def score_proposals(key_fields: dict, validated: dict) -> dict:
    """{field: {"correct": bool, "score": 0.0 | 1.0}} for one transcript."""
    out = {}
    for f in FIELDS:
        spec = key_fields[f]
        ok = proposal_correct(f, spec.get("expected_proposal"), predicted_proposal(f, validated[f], spec.get("crm_before")))
        out[f] = {"correct": ok, "score": 1.0 if ok else 0.0}
    return out


def expected_final_text(field: str, expected: dict, crm_before: Any) -> str:
    """The CRM text a reviewer would end up with if the expected proposal were applied (the value an oracle
    supplies when it edits)."""
    cur = fmt_value(field, crm_before)
    if field == "competitors":
        have = [dict(c) for c in (crm_before or [])]
        new = [c for c in expected["value"] if (c["name"], c["stance"]) not in {(h["name"], h["stance"]) for h in have}]
        return fmt_value(field, have + new)
    if field in ("pain_points", "use_case"):
        return "; ".join(parse_labels(cur) + [x for x in expected["value"] if x not in parse_labels(cur)])
    if field == "stage_signal":
        return f"Stage signal from call: {expected['value']}"
    return fmt_value(field, expected["value"])
