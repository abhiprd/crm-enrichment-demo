"""Scoring per SPEC section 6. A field is correct only if value and status both match the truth.

`score_field` returns credit in [0, 1]. Single-valued fields are 0 or 1. Competitors earn pair-level F1.
Next step earns the mean of action, owner, and date matches once status matches. The run-level
`field_score` is the mean credit over all (transcript, field) instances, so it is not a strict F1.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .schema import FIELDS

ALIASES = {"revenue.io": "revenueio", "chorus.ai": "chorus", "gong.io": "gong", "outreach.io": "outreach",
           "fireflies.ai": "fireflies", "avoma": "avoma"}


def norm_name(s: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(s or "").lower())).strip()


def norm_vendor(s: Any) -> str:
    raw = str(s or "").strip().lower()
    return ALIASES.get(raw) or norm_name(raw)


def to_amount(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    m = re.search(r"([\d,]*\.?\d+)\s*([kKmM]?)", str(v))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(round(n * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)))


def budget_equal(truth: Any, pred: Any) -> bool:
    if isinstance(truth, dict):
        return isinstance(pred, dict) and to_amount(pred.get("min")) == to_amount(truth["min"]) \
            and to_amount(pred.get("max")) == to_amount(truth["max"])
    return not isinstance(pred, dict) and to_amount(pred) == to_amount(truth)


_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def quarter_of(value: str) -> Optional[str]:
    """'2026-11-30' -> '2026-Q4'; a quarter string maps to itself; anything else is None."""
    v = str(value or "").strip().upper()
    if _QUARTER.match(v):
        return v
    m = _DATE.match(v)
    return f"{m.group(1)}-Q{(int(m.group(2)) - 1) // 3 + 1}" if m else None


def timeline_equal(truth: Any, pred: Any) -> bool:
    """SPEC section 6: the prediction must fall inside the truth period at the stated granularity.

    A quarter truth accepts that quarter or any date inside it. A date truth needs that exact day: a
    predicted quarter is coarser than the truth, so it never matches ("end of Q4" is not "Dec 31").
    """
    t, p = str(truth).strip().upper(), str(pred or "").strip().upper()
    if _QUARTER.match(t):
        return quarter_of(p) == t
    return p == t


def pairs(v: Any) -> set:
    out = set()
    for c in v or []:
        if isinstance(c, dict):
            out.add((norm_vendor(c.get("name")), str(c.get("stance", "")).lower()))
    return out


def competitor_f1(truth: Any, pred: Any) -> float:
    t, p = pairs(truth), pairs(pred)
    if not t and not p:
        return 1.0
    tp = len(t & p)
    if not tp:
        return 0.0
    prec, rec = tp / len(p), tp / len(t)
    return 2 * prec * rec / (prec + rec)


def next_step_parts(truth: Any, pred: Any) -> dict:
    pred = pred if isinstance(pred, dict) else {}
    return {"action": str(pred.get("action", "")).lower() == str(truth["action"]).lower(),
            "owner": norm_name(pred.get("owner")) == norm_name(truth["owner"]),
            "date": (pred.get("date") or None) == (truth.get("date") or None)}


def score_field(field: str, truth: dict, pred: dict) -> dict:
    """truth/pred are {"value", "status"} dicts. Returns {"score", "correct", ...}."""
    if truth["status"] != pred["status"]:
        return {"score": 0.0, "correct": False, "why": "status"}
    if truth["status"] == "not_mentioned":
        return {"score": 1.0, "correct": True}
    tv, pv = truth["value"], pred.get("value")
    if field == "budget":
        ok = budget_equal(tv, pv)
    elif field == "decision_timeline":
        ok = timeline_equal(tv, pv)
    elif field == "competitors":
        s = competitor_f1(tv, pv)
        return {"score": s, "correct": s == 1.0}
    elif field in ("economic_buyer", "champion"):
        ok = norm_name(tv) == norm_name(pv)
    elif field in ("pain_points", "use_case"):
        ok = set(tv) == set(pv if isinstance(pv, list) else [])
    elif field == "next_step":
        parts = next_step_parts(tv, pv)
        s = sum(parts.values()) / 3
        return {"score": s, "correct": s == 1.0, "parts": parts}
    elif field == "stage_signal":
        ok = str(pv).lower() == str(tv).lower()
    else:
        raise ValueError(field)
    return {"score": 1.0 if ok else 0.0, "correct": ok}


def score_extraction(key_fields: dict, extraction_fields: dict) -> dict:
    """key_fields: data/keys/<id>.json["fields"]. Returns {field: score dict}."""
    return {f: score_field(f, key_fields[f]["truth"], extraction_fields[f]) for f in FIELDS}


def field_score(scores: dict) -> float:
    return sum(s["score"] for s in scores.values()) / len(scores)
