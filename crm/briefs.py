"""Render transcript requests: the prompt a Claude chat or the transcript-writer agent works from."""

from __future__ import annotations

import json
from datetime import datetime

from .deals import DealSet
from .paths import Paths


def brief(deal: dict) -> dict:
    """What the writer needs to render the call. Nothing about CRM state, split, or proposals."""
    return {
        "deal_id": deal["deal_id"],
        "company": deal["company"],
        "call": deal["call"],
        "participants": deal["participants"],
        "cases": deal.get("cases", []),
        "facts": {f: spec["truth"] for f, spec in deal["fields"].items()},
    }


def select_deals(paths: Paths, dealset: DealSet, n: int, split: str | None = None,
                 deal_ids: list[str] | None = None, include_existing: bool = False) -> list[dict]:
    if deal_ids:
        missing = [d for d in deal_ids if d not in dealset.deals]
        if missing:
            raise SystemExit(f"unknown deal ids: {missing}")
        return [dealset.deals[d] for d in deal_ids]
    chosen = []
    for did in sorted(dealset.deals):
        d = dealset.deals[did]
        if split and d["split"] != split:
            continue
        if not include_existing and (paths.transcripts / f"{did}.md").exists():
            continue
        chosen.append(d)
        if len(chosen) >= n:
            break
    return chosen


def render_request(paths: Paths, dealset: DealSet, deals: list[dict]) -> str:
    template = paths.request_template.read_text(encoding="utf-8")
    payload = json.dumps([brief(d) for d in deals], indent=2)
    return (template
            .replace("{{VENDOR}}", json.dumps(dealset.vendor))
            .replace("{{DEAL_IDS}}", ", ".join(d["deal_id"] for d in deals))
            .replace("{{DEALS_JSON}}", payload))


def write_request(paths: Paths, dealset: DealSet, deals: list[dict]) -> str:
    paths.ensure()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = paths.requests / f"req_{stamp}_{deals[0]['deal_id']}-{deals[-1]['deal_id']}.md"
    out.write_text(render_request(paths, dealset, deals), encoding="utf-8")
    return str(out.relative_to(paths.root))
