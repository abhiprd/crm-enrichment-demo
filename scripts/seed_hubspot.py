"""Seed ~15 fictional deals into HubSpot from data/deals.json. Idempotent. Dry-run unless --live.

    python3 scripts/seed_hubspot.py            # print what would happen
    python3 scripts/seed_hubspot.py --live     # create the property group, properties, companies, deals
    python3 scripts/seed_hubspot.py --live --reset   # PATCH every seeded deal back to its crm_before values

HubSpot ids are written back to deals.json (hubspot.company_id / deal_id / seeded) so reruns update
instead of duplicating. Truth fields are never touched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm.config import Settings, load_env  # noqa: E402
from crm.hubspot import HubSpot, props_from_fields  # noqa: E402
from crm import preflight  # noqa: E402

DEAL_SUFFIX = " - Northbeam"


def before_values(deal: dict) -> dict:
    return {f: spec["crm_before"] for f, spec in deal["fields"].items()}


def seed(data: dict, hs: HubSpot, owner_id: str, reset: bool) -> int:
    seeded = [d for d in data["deals"] if d["hubspot"].get("seed")]
    hs.ensure_properties()
    new = [d for d in seeded if not d["hubspot"].get("deal_id")]
    if new and not reset:
        companies = hs.create_companies([d["company"] for d in new])
        for d in new:
            d["hubspot"]["company_id"] = companies.get(d["company"]["domain"])
        creatable = [d for d in new if d["hubspot"]["company_id"] or not hs.live]
        ids = hs.create_deals([{"name": d["company"]["name"] + DEAL_SUFFIX, "props": props_from_fields(before_values(d)),
                                "company_id": d["hubspot"]["company_id"] or "DRY_RUN", "owner_id": owner_id}
                               for d in creatable])
        for d in creatable:
            did = ids.get(d["company"]["name"] + DEAL_SUFFIX)
            if did:
                d["hubspot"]["deal_id"], d["hubspot"]["seeded"] = did, True
    for d in seeded:
        if d["hubspot"].get("deal_id") and (reset or d not in new):
            hs.patch_deal(d["hubspot"]["deal_id"], props_from_fields(before_values(d)))
    return len(seeded)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually call HubSpot (default: dry-run)")
    ap.add_argument("--reset", action="store_true", help="restore every seeded deal to its crm_before values")
    ap.add_argument("--deals", default=str(ROOT / "data" / "deals.json"))
    args = ap.parse_args()

    load_env(ROOT)
    s = Settings.load()
    if args.live and not s.hubspot_key:
        print("HUBSPOT_SERVICE_KEY is not set in .env")
        return 1
    if args.live:
        results = preflight.run(s, ["hubspot"])
        if not preflight.passed(results):
            print(preflight.render(results))
            print("\npreflight FAILED: no HubSpot writes were attempted")
            return 1
    path = Path(args.deals)
    data = json.loads(path.read_text(encoding="utf-8"))
    hs = HubSpot(s.hubspot_key, live=args.live)
    owner = s.hubspot_owner_id or (hs.default_owner_id() if s.hubspot_key else "")
    try:
        n = seed(data, hs, owner, args.reset)
    except Exception as e:  # report API errors without a traceback that might echo headers
        print(f"seed failed: {e}")
        return 1
    if args.live:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"seeded {n} deals; ids written to {path.name}")
    else:
        print(f"dry-run: {n} deals selected, {len(hs.planned)} write call(s) planned (pass --live to run)")
        for method, p, _ in hs.planned[:8]:
            print(f"  {method} {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
