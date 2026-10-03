"""M0 vertical slice: one hardcoded proposal, reviewed in Slack, written to HubSpot, restored by --reset.

Dry-run (default): builds the card, simulates an approve against an in-memory HubSpot stand-in, and prints
the result. --live posts to Slack, serves button clicks, and writes to the real HubSpot deal.
"""

from __future__ import annotations

import json
import sys

from . import db
from .config import Settings
from .deals import load_deals
from .hubspot import HubSpot, deal_url, fmt_value
from .paths import Paths
from .review import card_blocks, create_proposal, get_proposal, handle_decision

PROPERTY = "amount"
PROPOSED = "60000"
QUOTE = {"quote": "we've set aside about sixty grand for this", "ts": "12:04"}


class FakeHubSpot:
    """In-memory stand-in so the slice runs end to end with no credentials."""

    def __init__(self, value: str):
        self.value, self.live, self.writes = value, False, []

    def get_deal(self, deal_id, props):
        return {PROPERTY: self.value}

    def patch_deal(self, deal_id, props):
        self.writes.append(props)
        self.value = props[PROPERTY]


def evidence_for(deal: dict) -> dict:
    """The hardcoded quote, attributed to the deal's first external participant."""
    speaker = next((p["name"] for p in deal["participants"] if not p["internal"]), "Unknown speaker")
    return {**QUOTE, "speaker": speaker}


def context_for(deal: dict, hs=None) -> dict:
    """Account and deal names with ids, and a link to the deal record when the portal id is known."""
    h = deal["hubspot"]
    ctx = {"account_name": deal["company"]["name"], "account_id": h.get("company_id") or "dry-run",
           "deal_name": deal["company"]["name"] + " - Northbeam", "deal_id": h.get("deal_id") or "dry-run"}
    acct = hs.account() if hs is not None else {}
    if acct.get("portalId") and h.get("deal_id"):
        ctx["deal_url"] = deal_url(acct["portalId"], h["deal_id"], acct.get("uiDomain") or "app.hubspot.com")
    return ctx


def pick_deal(paths: Paths, want: str, live: bool) -> dict:
    deals = [d for d in load_deals(paths).deals.values() if d.get("hubspot", {}).get("seed")]
    if want:
        deals = [d for d in deals if d["deal_id"] == want]
    if live:
        deals = [d for d in deals if d["hubspot"].get("deal_id")]
    if not deals:
        raise SystemExit("no seeded deal found. Run scripts/seed_hubspot.py --live first." if live
                         else "no deal marked for seeding in deals.json")
    return deals[0]


def run(paths: Paths, deal_id: str = "", live: bool = False, settings: Settings = None) -> int:
    settings = settings or Settings.load()
    deal = pick_deal(paths, deal_id, live)
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    before = fmt_value("budget", deal["fields"]["budget"]["crm_before"])

    if not live:
        hs = FakeHubSpot(before)
        pid = create_proposal(conn, deal_id=deal["deal_id"], hubspot_id="fake", prop=PROPERTY, current=before,
                              proposed=PROPOSED, evidence=evidence_for(deal), context=context_for(deal))
        print(json.dumps(card_blocks(get_proposal(conn, pid)), indent=2))
        out = handle_decision(conn, hs, pid, "approve", "U_DRYRUN", ())
        print(f"approve -> {out.status}: {out.message}")
        again = handle_decision(conn, hs, pid, "approve", "U_DRYRUN", ())
        print(f"second click -> {again.status}: {again.message}  (writes: {len(hs.writes)})")
        return 0 if out.status == "approved" and len(hs.writes) == 1 else 1

    missing = settings.missing("hubspot_key", "slack_bot_token", "slack_app_token", "slack_channel")
    if missing:
        print(f"missing in .env: {', '.join(missing)}")
        return 1
    if not settings.slack_reviewer_ids:
        print("SLACK_REVIEWER_IDS must be set for --live (click authorization)")
        return 1
    from . import preflight
    results = preflight.run(settings, ["hubspot", "slack"])
    if not preflight.passed(results):
        print(preflight.render(results))
        print("\npreflight FAILED: nothing was posted or written")
        return 1
    from .slack_app import post_card, serve
    hs = HubSpot(settings.hubspot_key, live=True)
    current = hs.get_deal(deal["hubspot"]["deal_id"], [PROPERTY]).get(PROPERTY) or ""
    pid = create_proposal(conn, deal_id=deal["deal_id"], hubspot_id=deal["hubspot"]["deal_id"], prop=PROPERTY,
                          current=current, proposed=PROPOSED, evidence=evidence_for(deal), channel=settings.slack_channel,
                          context=context_for(deal, hs))
    conn.execute("UPDATE proposals SET interaction_id = ? WHERE id = ?", (f"slice-{pid}", pid))
    conn.commit()
    post_card(settings, conn, f"slice-{pid}")
    print(f"card posted for {deal['deal_id']} ({deal['company']['name']}); waiting for a click. Ctrl-C to stop.")
    serve(settings, paths.root / "results" / "crm.sqlite", hs)
    return 0


if __name__ == "__main__":
    sys.exit(run(Paths.from_env()))
