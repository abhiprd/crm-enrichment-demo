"""Generate data/deals.json: 100 fictional deals, seeded and stratified 40/40/20.

Truth comes first. Each deal sets true field values, a CRM relationship per field, and the traps and
house rules its call must carry. `expected_proposal` is what should land in the CRM after house rules,
and it is computed here, so the rules apply to every deal and not only the labeled ones.

    python3 scripts/make_deals.py [--seed 1337] [--out data/deals.json] [--check]

Replaces the three hand-written example deals. Deterministic for a given seed.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crm.deals import validate_deal  # noqa: E402
from crm.schema import FIELDS  # noqa: E402

N_DEALS = 100
SPLIT_CYCLE = ["learn", "learn", "validation", "validation", "test"]  # 40/40/20 over 100
CLEAN_SHARE = 20
SEED_COUNTS = {"learn": 6, "validation": 6, "test": 3}  # ~15 deals seeded into HubSpot

VENDOR = {
    "name": "Northbeam",
    "domain": "northbeam.example",
    "sells": "conversation intelligence and forecasting software for mid-market sales teams",
}
INTERNAL = [
    ("Dana Reyes", "Account Executive"),
    ("Marcus Lee", "Solutions Engineer"),
    ("Jo Whitfield", "Account Executive"),
    ("Ravi Nair", "Solutions Engineer"),
]
FIRST = ["Priya", "Tom", "Elena", "Sam", "Lina", "Omar", "Hannah", "Diego", "Mei", "Callum", "Aisha", "Greg",
         "Noor", "Felix", "Tamsin", "Jonas", "Ines", "Wes", "Kavya", "Ruben", "Sofia", "Dev", "Maren", "Isaac",
         "Yuki", "Bram", "Carla", "Idris", "Nadia", "Leon", "Opal", "Tariq", "Greta", "Hugo", "Zainab", "Pierce"]
LAST = ["Shah", "Alvarez", "Brooks", "Okafor", "Park", "Haddad", "Lindqvist", "Moreno", "Tan", "Whitaker",
        "Bello", "Novak", "Rahman", "Ostrowski", "Fenwick", "Kruger", "Duarte", "Ellis", "Menon", "Castillo",
        "Varga", "Sato", "Nyberg", "Qureshi", "Dubois", "Achebe", "Lund", "Ferreira", "Kaplan", "Oyelaran"]
CO_A = ["Kestrel", "Harborvine", "Copperline", "Alder", "Brightwater", "Cinder", "Driftwood", "Evergreen",
        "Fernhill", "Granite", "Hollis", "Ironbark", "Juniper", "Kiln", "Lantern", "Marlow", "Nettle",
        "Orchard", "Pinecrest", "Quarry", "Redwood", "Saltmarsh", "Thistle", "Umber", "Vantage", "Willow",
        "Yarrow", "Zephyr", "Amberly", "Bluestem"]
INDUSTRIES = [("Foods", "food distribution"), ("Logistics", "freight brokerage"),
              ("Dental Group", "dental practice management"), ("Health", "regional healthcare staffing"),
              ("Supply", "industrial supply"), ("Software", "vertical SaaS"), ("Solar", "solar installation"),
              ("Insurance", "independent insurance agency"), ("Telecom", "business telecom reseller"),
              ("Materials", "building materials"), ("Education", "ed-tech services"),
              ("Mobility", "fleet leasing")]
CHAMPION_ROLES = ["VP Revenue Operations", "Director of Sales", "Sales Enablement Lead", "Head of Sales Operations"]
BUYER_ROLES = ["CFO", "COO", "CRO", "VP Sales"]
SKEPTIC_ROLES = ["IT Director", "Controller", "Director of Security"]
COMPETITORS = ["Gong", "Clari", "Chorus", "Salesloft", "Outreach", "Avoma", "Fireflies", "Revenue.io"]
PAIN = ["forecast_accuracy", "pipeline_visibility", "deal_slippage", "rep_ramp_time", "coaching_at_scale",
        "manual_data_entry", "data_quality", "tool_sprawl"]
USE = ["call_recording", "call_coaching", "forecasting", "deal_inspection", "crm_hygiene",
       "pipeline_reporting", "onboarding", "competitive_intel"]
ACTIONS = ["send_security_docs", "send_proposal", "send_contract", "schedule_demo", "schedule_followup",
           "technical_review", "pilot_kickoff", "intro_to_buyer"]
INTERNAL_OWNER_ACTIONS = {"send_security_docs", "send_proposal", "send_contract", "schedule_followup", "pilot_kickoff"}

# slot -> case options. One case per slot per deal keeps cases from fighting over a field.
SLOTS = {
    "competitors": ["negation", "competitor_threshold"],
    "decision_timeline": ["hedged_timeline", "quarter_timeline"],
    "budget": ["ballpark_budget", "superseded_budget"],
    "next_step": ["dated_next_step", "buried_next_step", "superseded_next_step"],
    "champion": ["speaker_attribution", "committed_champion"],
}
CASE_OF = {"superseded_budget": "superseded_value", "superseded_next_step": "superseded_value"}
MENTION_P = {"budget": 0.6, "decision_timeline": 0.7, "competitors": 0.5, "economic_buyer": 0.6,
             "champion": 0.75, "pain_points": 0.9, "use_case": 0.8, "next_step": 0.9}
CRM_P = (("matches", 0.2), ("stale", 0.3), ("empty", 0.5))


def quarter(d: str) -> str:
    if "Q" in d:
        return d
    y, m, _ = d.split("-")
    return f"{y}-Q{(int(m) - 1) // 3 + 1}"


def pick_relation(rng: random.Random) -> str:
    r, acc = rng.random(), 0.0
    for name, p in CRM_P:
        acc += p
        if r < acc:
            return name
    return "empty"


def future_quarters(call_date: date) -> list[str]:
    q = (call_date.month - 1) // 3 + 1
    out, y = [], call_date.year
    for _ in range(5):
        out.append(f"{y}-Q{q}")
        q += 1
        if q == 5:
            q, y = 1, y + 1
    return out[1:] if len(out) > 1 else out


def quarter_end(q: str) -> date:
    y, n = q.split("-Q")
    month = int(n) * 3
    last = {3: 31, 6: 30, 9: 30, 12: 31}[month]
    return date(int(y), month, last)


def next_step_value(rng: random.Random, call_date: date, internal: list[dict], external: list[dict],
                    dated: bool = True) -> dict:
    action = rng.choice(ACTIONS)
    pool = internal if action in INTERNAL_OWNER_ACTIONS else internal + external
    owner = rng.choice(pool)["name"]
    when = (call_date + timedelta(days=rng.randint(3, 21))).isoformat() if dated else None
    return {"action": action, "owner": owner, "date": when}


def person_names(rng: random.Random, used: set[str], n: int) -> list[str]:
    out = []
    while len(out) < n:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if name not in used:
            used.add(name)
            out.append(name)
    return out


def plan_cases(rng: random.Random, n: int) -> list[list[str]]:
    """Case lists per deal: CLEAN_SHARE clean, the rest 2-3 cases, balanced across options."""
    plans: list[list[str]] = [[] for _ in range(CLEAN_SHARE)]
    use = {c: 0 for opts in SLOTS.values() for c in opts}
    for _ in range(n - CLEAN_SHARE):
        slots = rng.sample(list(SLOTS), rng.choice([2, 2, 3]))
        chosen = []
        for s in slots:
            opts = sorted(SLOTS[s], key=lambda c: (use[c], rng.random()))
            use[opts[0]] += 1
            chosen.append(opts[0])
        plans.append(chosen)
    rng.shuffle(plans)
    return plans


def build_deal(rng: random.Random, idx: int, case_list: list[str], used_people: set[str],
               used_companies: set[str]) -> dict:
    deal_id = f"d{idx:03d}"
    call_date = date(2026, 7, 1) + timedelta(days=rng.randint(0, 85))
    call_type = rng.choice(["discovery", "demo", "negotiation"])

    while True:
        stem, (suffix, industry) = rng.choice(CO_A), rng.choice(INDUSTRIES)
        company = f"{stem} {suffix}"
        if company not in used_companies:
            used_companies.add(company)
            break
    domain = f"{stem.lower()}{suffix.split()[0].lower()}.example"

    n_ext = 3 if "speaker_attribution" in case_list or rng.random() < 0.5 else 2
    names = person_names(rng, used_people, n_ext)
    external = [{"name": names[0], "role": rng.choice(CHAMPION_ROLES), "org": company, "internal": False},
                {"name": names[1], "role": rng.choice(BUYER_ROLES), "org": company, "internal": False}]
    if n_ext == 3:
        external.append({"name": names[2], "role": rng.choice(SKEPTIC_ROLES), "org": company, "internal": False})
    internal_people = rng.sample(INTERNAL, rng.choice([1, 2]))
    internal = [{"name": n, "role": r, "org": VENDOR["name"], "internal": True} for n, r in internal_people]
    participants = internal + external
    champion, buyer = external[0]["name"], external[1]["name"]
    skeptic = external[2]["name"] if n_ext == 3 else None
    ae = internal[0]["name"]

    cases: list[dict] = []
    flags: set[str] = set()

    def add_case(case: str, field: str, note: str) -> None:
        cid = CASE_OF.get(case, case)
        cases.append({"id": cid, "field": field, "note": note})
        flags.add(case)

    # --- truth per field ---------------------------------------------------------------------
    truth: dict[str, dict] = {}
    for f in FIELDS:
        truth[f] = {"value": None, "status": "not_mentioned"}
    forced = {
        "budget": {"ballpark_budget", "superseded_budget"}, "decision_timeline": {"hedged_timeline", "quarter_timeline"},
        "competitors": {"negation", "competitor_threshold"},
        "next_step": {"dated_next_step", "buried_next_step", "superseded_next_step"},
        "champion": {"speaker_attribution", "committed_champion"},
    }
    mentioned = {f for f in FIELDS if f == "stage_signal" or rng.random() < MENTION_P.get(f, 0)}
    for f, opts in forced.items():
        if opts & set(case_list):
            mentioned.add(f)
    fut = future_quarters(call_date)

    for f in FIELDS:
        if f not in mentioned:
            continue
        if f == "budget":
            val = rng.randrange(25, 251, 5) * 1000
            truth[f] = {"value": val, "status": "stated"}
        elif f == "decision_timeline":
            if rng.random() < 0.5:
                truth[f] = {"value": quarter_end(rng.choice(fut)).isoformat()[:8] + "15", "status": "stated"}
            else:
                truth[f] = {"value": rng.choice(fut), "status": "stated"}
        elif f == "competitors":
            names_c = rng.sample(COMPETITORS, rng.choice([1, 1, 2]))
            truth[f] = {"value": [{"name": c, "stance": rng.choice(["evaluating", "incumbent"])} for c in names_c],
                        "status": "stated"}
        elif f == "economic_buyer":
            truth[f] = {"value": buyer, "status": "stated"}
        elif f == "champion":
            truth[f] = {"value": champion, "status": "stated"}
        elif f == "pain_points":
            truth[f] = {"value": sorted(rng.sample(PAIN, rng.choice([1, 1, 2]))), "status": "stated"}
        elif f == "use_case":
            truth[f] = {"value": sorted(rng.sample(USE, rng.choice([1, 1, 2]))), "status": "stated"}
        elif f == "next_step":
            truth[f] = {"value": next_step_value(rng, call_date, internal, external), "status": "stated"}
        elif f == "stage_signal":
            truth[f] = {"value": rng.choices(["advance", "hold", "regress"], [5, 3, 1])[0], "status": "stated"}

    # --- apply cases -------------------------------------------------------------------------
    for case in case_list:
        if case == "negation":
            ruled = rng.choice(COMPETITORS)
            truth["competitors"] = {"value": [{"name": ruled, "stance": "ruled_out"}], "status": "negated"}
            speaker = rng.choice(external)["name"]
            add_case(case, "competitors",
                     f"{speaker} says they looked at {ruled} earlier this year and are not looking at it anymore. "
                     f"Nobody labels it a negation.")
        elif case == "competitor_threshold":
            real = rng.choice(COMPETITORS)
            passing = rng.choice([c for c in COMPETITORS if c != real])
            truth["competitors"] = {"value": [{"name": real, "stance": "evaluating"}], "status": "stated"}
            add_case(case, "competitors",
                     f"{rng.choice(external)['name']} says they are actively comparing {real} against us. "
                     f"Separately, {rng.choice(external)['name']} mentions in passing that a friend's company uses "
                     f"{passing}. Nobody suggests evaluating {passing}.")
        elif case == "hedged_timeline":
            q = rng.choice(fut)
            truth["decision_timeline"] = {"value": q, "status": "hedged"}
            add_case(case, "decision_timeline",
                     f"{champion} says they'd probably decide around {q.replace('-', ' ')} if the pilot goes well. "
                     f"Nobody commits to a date.")
        elif case == "quarter_timeline":
            d = quarter_end(rng.choice(fut)) - timedelta(days=rng.randint(10, 40))
            truth["decision_timeline"] = {"value": d.isoformat(), "status": "stated"}
            add_case(case, "decision_timeline",
                     f"{buyer} states a firm decision date of {d.isoformat()}, stated as a day, not a quarter.")
        elif case == "ballpark_budget":
            lo = rng.randrange(40, 160, 10) * 1000
            hi = lo + rng.choice([20, 30, 40]) * 1000
            truth["budget"] = {"value": {"min": lo, "max": hi}, "status": "hedged"}
            add_case(case, "budget",
                     f"{buyer} gives a rough ballpark of {lo // 1000} to {hi // 1000} thousand and says the real "
                     f"number depends on the board. They call it a ballpark in their own words.")
        elif case == "superseded_budget":
            final = rng.randrange(40, 200, 5) * 1000
            first = final + rng.choice([-20, 20, 30]) * 1000
            truth["budget"] = {"value": final, "status": "superseded"}
            add_case(case, "budget",
                     f"{buyer} first says the budget is {first // 1000} thousand. Later, after checking with "
                     f"finance, they correct it to {final // 1000} thousand. {final // 1000} thousand is final.")
        elif case == "dated_next_step":
            v = next_step_value(rng, call_date, internal, external, dated=False)
            truth["next_step"] = {"value": v, "status": "stated"}
            add_case(case, "next_step",
                     f"Both sides agree on a next step ({v['action'].replace('_', ' ')}, owner {v['owner']}) "
                     f"but nobody gives or agrees on a date.")
        elif case == "buried_next_step":
            v = next_step_value(rng, call_date, internal, external)
            truth["next_step"] = {"value": v, "status": "stated"}
            add_case(case, "next_step",
                     f"Late in the call, in passing, {v['owner']} commits to {v['action'].replace('_', ' ')} by "
                     f"{v['date']}. The call then moves on to something else.")
        elif case == "superseded_next_step":
            v = next_step_value(rng, call_date, internal, external)
            first = (date.fromisoformat(v["date"]) - timedelta(days=rng.choice([4, 7, 10]))).isoformat()
            truth["next_step"] = {"value": v, "status": "superseded"}
            add_case(case, "next_step",
                     f"{v['owner']} first commits to {v['action'].replace('_', ' ')} by {first}. Near the end it "
                     f"moves to {v['date']} and everyone agrees. {v['date']} is final.")
        elif case == "speaker_attribution":
            truth["champion"] = {"value": champion, "status": "stated"}
            add_case(case, "champion",
                     f"{champion} is the advocate and pushes for the purchase. {skeptic} raises doubts and makes "
                     f"negative remarks about cost and rollout. Keep who said what clearly separable.")
        elif case == "committed_champion":
            truth["champion"] = {"value": champion, "status": "stated"}
            add_case(case, "champion",
                     f"{champion} is enthusiastic and positive throughout but never commits to any internal "
                     f"action (no intro to the buyer, no internal sponsorship, no owned follow-up).")

    # --- CRM relationship and expected proposal ---------------------------------------------
    fields: dict[str, dict] = {}
    for f in FIELDS:
        t = truth[f]
        before, relation = crm_before_for(rng, f, t, flags, call_date, external, internal)
        fields[f] = {"truth": t, "expected_proposal": expected_proposal(f, t, before, flags),
                     "crm_relation": relation, "crm_before": before}

    return {
        "deal_id": deal_id, "split": None,
        "company": {"name": company, "domain": domain, "industry": industry,
                    "employees": rng.choice([60, 120, 180, 250, 420, 650, 900, 1500])},
        "call": {"type": call_type, "date": call_date.isoformat(),
                 "target_turns": rng.choice([32, 36, 40, 44, 48])},
        "participants": participants, "cases": cases, "fields": fields,
        "hubspot": {"seed": False, "seeded": False, "company_id": None, "deal_id": None},
    }


def empty_before(f: str):
    return [] if f in ("competitors", "pain_points", "use_case") else None


def crm_before_for(rng, f, t, flags, call_date, external, internal):
    """Return (crm_before, crm_relation). Not-mentioned fields are always empty in the CRM."""
    if t["status"] == "not_mentioned":
        return empty_before(f), "empty"
    if f == "stage_signal":
        return None, "empty"
    rel = pick_relation(rng)
    v = t["value"]
    if f == "budget":
        if isinstance(v, dict) or t["status"] == "hedged":
            rel = "empty" if rel == "matches" else rel
        if rel == "matches":
            return v, rel
        if rel == "stale":
            return int(round(rng.choice([0.5, 0.75, 1.5, 2]) * (v if isinstance(v, int) else v["min"]), -3)), rel
        return None, rel
    if f == "decision_timeline":
        if rel == "matches" and "Q" not in v:
            rel = "stale"  # a date truth is conformed to a quarter, so an exact match would still propose
        if rel == "matches":
            return v, rel
        if rel == "stale":
            q = rng.choice([x for x in future_quarters(call_date + timedelta(days=300)) if x != quarter(v)])
            return (quarter_end(q).isoformat() if rng.random() < 0.5 else q), rel
        return None, rel
    if f == "competitors":
        if rel == "matches":
            return list(v), rel
        if rel == "stale":
            first = v[0]
            return [{"name": first["name"], "stance": "evaluating" if first["stance"] != "evaluating"
                     else "incumbent"}], rel
        return [], rel
    if f in ("economic_buyer", "champion"):
        if rel == "matches":
            return v, rel
        if rel == "stale":
            others = [p["name"] for p in external if p["name"] != v]
            return rng.choice(others), rel
        return None, rel
    if f in ("pain_points", "use_case"):
        pool = PAIN if f == "pain_points" else USE
        if rel == "matches":
            return list(v), rel
        if rel == "stale":
            return [rng.choice([x for x in pool if x not in v])], rel
        return [], rel
    if f == "next_step":
        if rel == "matches":
            return dict(v), rel
        if rel == "stale":
            old = rng.choice([a for a in ACTIONS if a != v["action"]])
            return {"action": old, "owner": v["owner"], "date": (call_date - timedelta(days=2)).isoformat()}, rel
        return None, rel
    return None, "empty"


def expected_proposal(f: str, t: dict, before, flags: set[str]):
    """What should land in the CRM after house rules. None means no proposal."""
    if t["status"] == "not_mentioned" or t["value"] is None:
        return None
    v = t["value"]
    if f == "stage_signal":
        return {"action": "note", "value": v}
    if f == "budget":
        if "ballpark_budget" in flags:
            return None
        return None if v == before else {"action": "set", "value": v}
    if f == "decision_timeline":
        q = quarter(v)  # quarter_timeline applies to every deal
        if before and q == quarter(before):
            return None
        out = {"action": "set", "value": q}
        if t["status"] == "hedged":
            out["tentative"] = True
        return out
    if f == "competitors":
        have = {(c["name"], c["stance"]) for c in before}
        new = [c for c in v if (c["name"], c["stance"]) not in have]
        return {"action": "append", "value": new} if new else None
    if f == "champion":
        if "committed_champion" in flags:
            return None
        return None if v == before else {"action": "set", "value": v}
    if f == "economic_buyer":
        return None if v == before else {"action": "set", "value": v}
    if f in ("pain_points", "use_case"):
        new = [x for x in v if x not in before]
        return {"action": "append", "value": new} if new else None
    if f == "next_step":
        if v.get("date") is None:  # dated_next_step applies to every deal
            return None
        return None if v == before else {"action": "set", "value": v}
    return None


def assign_splits_and_seeds(rng: random.Random, deals: list[dict]) -> None:
    """Deal out splits so every split shares the same case and CRM-relation mix, then mark seeds."""
    def stratum(d: dict) -> tuple:
        cases = tuple(sorted(c["id"] for c in d["cases"])) or ("clean",)
        stale = sum(f["crm_relation"] == "stale" for f in d["fields"].values())
        return (cases[0], min(stale, 3), d["deal_id"])

    ordered = sorted(deals, key=stratum)
    for i, d in enumerate(ordered):
        d["split"] = SPLIT_CYCLE[i % len(SPLIT_CYCLE)]
    for split, n in SEED_COUNTS.items():
        pool = [d for d in deals if d["split"] == split]
        # prefer deals with prefilled or stale CRM values so seeded diffs look real
        pool.sort(key=lambda d: (-sum(f["crm_relation"] != "empty" for f in d["fields"].values()), rng.random()))
        for d in pool[:n]:
            d["hubspot"]["seed"] = True


def build_all(seed: int) -> dict:
    rng = random.Random(seed)
    plans = plan_cases(rng, N_DEALS)
    used_people: set[str] = set()
    used_companies: set[str] = set()
    deals = [build_deal(rng, i + 1, plans[i], used_people, used_companies) for i in range(N_DEALS)]
    assign_splits_and_seeds(rng, deals)
    return {"_comment": f"Generated by scripts/make_deals.py --seed {seed}. Truth lives here and nowhere else. "
                        "Seeded HubSpot ids are written back by scripts/seed_hubspot.py.",
            "vendor": VENDOR, "deals": deals}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "deals.json"))
    ap.add_argument("--check", action="store_true", help="validate and summarize, do not write")
    ap.add_argument("--force", action="store_true", help="overwrite an existing 100-deal file")
    args = ap.parse_args()

    data = build_all(args.seed)
    errs = [e for d in data["deals"] for e in validate_deal(d)]
    if errs:
        print("\n".join(errs))
        return 1
    split_n = {s: sum(d["split"] == s for d in data["deals"]) for s in ("learn", "validation", "test")}
    clean = sum(not d["cases"] for d in data["deals"])
    print(f"{len(data['deals'])} deals, splits {split_n}, clean {clean}, "
          f"seeded {sum(d['hubspot']['seed'] for d in data['deals'])}")
    if args.check:
        return 0
    out = Path(args.out)
    if out.exists():
        existing = json.loads(out.read_text(encoding="utf-8"))
        has_ids = any(d.get("hubspot", {}).get("deal_id") for d in existing.get("deals", []))
        if has_ids and not args.force:
            print("refusing to overwrite: deals.json holds HubSpot ids. Use --force to regenerate.")
            return 1
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
