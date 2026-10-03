"""Proposal builder: an extraction plus the deal's current CRM values becomes zero or more proposed writes.

Pure and typed. Follows the SPEC section 3 status table: stated is proposed; hedged is proposed with a
tentative tag; negated is proposed only if it changes what the CRM says; superseded proposes the final value;
not_mentioned and unchanged values propose nothing. Stage signal becomes a deal note, never a stage change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from .hubspot import PROPERTY, fmt_value
from .scoring import norm_name, norm_vendor, to_amount

LIST_FIELDS = ("pain_points", "use_case")
NOTE_PROPERTY = "note"


@dataclass
class Proposal:
    field: str
    prop: str  # HubSpot property, or "note"
    action: str  # set | append | clear | note
    current: str  # the CRM text now
    proposed: str  # the new CRM text (set/append/clear) or the note body
    tentative: bool = False
    status: str = "stated"


_PAIR = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def parse_competitors(text: str) -> list:
    """'Gong (evaluating); Clari (incumbent)' -> [{'name','stance'}]. A bare name gets an empty stance."""
    out = []
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        m = _PAIR.match(part)
        out.append({"name": m.group(1).strip(), "stance": m.group(2).strip()} if m else {"name": part, "stance": ""})
    return out


def parse_labels(text: str) -> list:
    return [x.strip() for x in (text or "").split(";") if x.strip()]


def merge_competitors(current: list, new: list) -> list:
    """Add vendors that are absent; a vendor already listed takes the new stance. Order is kept."""
    out = [dict(c) for c in current]
    for c in new:
        hit = next((o for o in out if norm_vendor(o["name"]) == norm_vendor(c.get("name"))), None)
        if hit is None:
            out.append({"name": c["name"], "stance": c.get("stance", "")})
        else:
            hit["stance"] = c.get("stance") or hit["stance"]
    return out


def _same_text(field: str, a: str, b: str) -> bool:
    if field == "budget":
        return to_amount(a) is not None and to_amount(a) == to_amount(b)
    return norm_name(a) == norm_name(b)


def _single(field: str, spec: dict, current: str, tentative: bool) -> Optional[Proposal]:
    status, value = spec["status"], spec["value"]
    prop = PROPERTY[field]
    if status == "negated":  # a negated single value only matters if the CRM still holds something
        return Proposal(field, prop, "clear", current, "", tentative, status) if current.strip() else None
    if field == "next_step":
        value = value[0] if isinstance(value, list) and value else (value if isinstance(value, dict) else None)
    text = fmt_value(field, value)
    if not text or _same_text(field, text, current):
        return None
    return Proposal(field, prop, "set", current, text, tentative, status)


def _competitors(spec: dict, current: str, tentative: bool) -> Optional[Proposal]:
    have = parse_competitors(current)
    new = [c for c in (spec["value"] or []) if isinstance(c, dict) and c.get("name")]
    if spec["status"] == "negated":  # the buyer dropped a vendor: record it as ruled_out
        new = [{"name": c["name"], "stance": "ruled_out"} for c in new]
    merged = merge_competitors(have, new)
    if merged == have:
        return None
    return Proposal("competitors", PROPERTY["competitors"], "append", current, fmt_value("competitors", merged),
                    tentative, spec["status"])


def _labels(field: str, spec: dict, current: str, tentative: bool) -> Optional[Proposal]:
    have = parse_labels(current)
    new = [x for x in (spec["value"] or []) if x not in have]
    if spec["status"] == "negated" or not new:
        return None
    return Proposal(field, PROPERTY[field], "append", current, "; ".join(have + new), tentative, spec["status"])


def build_proposals(extraction: dict, current: dict) -> list:
    """extraction: field -> {value, status, evidence} (after quote validation). current: field -> CRM text."""
    out: list = []
    for field, spec in extraction.items():
        status = spec["status"]
        if status == "not_mentioned":
            continue
        tentative = status == "hedged"
        cur = current.get(field, "") or ""
        if field == "stage_signal":
            if spec["value"]:
                out.append(Proposal(field, NOTE_PROPERTY, "note", "", f"Stage signal from call: {spec['value']}",
                                    tentative, status))
            continue
        if field == "competitors":
            p = _competitors(spec, cur, tentative)
        elif field in LIST_FIELDS:
            p = _labels(field, spec, cur, tentative)
        else:
            p = _single(field, spec, cur, tentative)
        if p is not None:
            out.append(p)
    return out
