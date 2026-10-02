"""Load and sanity-check data/deals.json, the single source of truth for the eval set."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .paths import Paths
from .schema import CALL_TYPES, CASE_IDS, CRM_RELATIONS, FIELDS, SPLITS, STATUSES


@dataclass
class DealSet:
    vendor: dict
    deals: dict[str, dict]


def load_deals(paths: Paths) -> DealSet:
    if not paths.deals.exists():
        return DealSet(vendor={}, deals={})
    raw = json.loads(paths.deals.read_text(encoding="utf-8"))
    deals = {d["deal_id"]: d for d in raw.get("deals", [])}
    if len(deals) != len(raw.get("deals", [])):
        raise ValueError("deals.json has duplicate deal_id values")
    return DealSet(vendor=raw.get("vendor", {}), deals=deals)


def validate_deal(deal: dict) -> list[str]:
    """Structural checks only. Whether the truth is sensible is a human call."""
    errs: list[str] = []
    did = deal.get("deal_id", "<no id>")
    if deal.get("split") not in SPLITS:
        errs.append(f"{did}: split must be one of {SPLITS}")
    call = deal.get("call", {})
    if call.get("type") not in CALL_TYPES:
        errs.append(f"{did}: call.type must be one of {CALL_TYPES}")
    if not call.get("date"):
        errs.append(f"{did}: call.date missing")
    parts = deal.get("participants", [])
    if len(parts) < 2:
        errs.append(f"{did}: needs at least 2 participants")
    if not any(p.get("internal") for p in parts) or all(p.get("internal") for p in parts):
        errs.append(f"{did}: needs at least one internal and one external participant")
    fields = deal.get("fields", {})
    missing = [f for f in FIELDS if f not in fields]
    if missing:
        errs.append(f"{did}: fields missing {missing}")
    for f, spec in fields.items():
        if f not in FIELDS:
            errs.append(f"{did}: unknown field {f}")
            continue
        status = spec.get("truth", {}).get("status")
        if status not in STATUSES:
            errs.append(f"{did}.{f}: truth.status must be one of {STATUSES}")
        if spec.get("crm_relation") not in CRM_RELATIONS:
            errs.append(f"{did}.{f}: crm_relation must be one of {CRM_RELATIONS}")
        if "expected_proposal" not in spec:
            errs.append(f"{did}.{f}: expected_proposal missing (use null for no proposal)")
    for case in deal.get("cases", []):
        if case.get("id") not in CASE_IDS:
            errs.append(f"{did}: unknown case id {case.get('id')}")
        if case.get("field") not in FIELDS:
            errs.append(f"{did}: case {case.get('id')} names unknown field {case.get('field')}")
        if not case.get("note"):
            errs.append(f"{did}: case {case.get('id')} needs a render note")
    return errs
