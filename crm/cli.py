"""CLI entry point: python -m crm <command>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import briefs
from .config import load_env
from .deals import load_deals, validate_deal
from .ingest import format_result, ingest_inbox, watch
from .paths import Paths
from .schema import SPLITS


def cmd_ingest(args, paths: Paths) -> int:
    if args.live and not args.run:
        print("--live only applies with --run")
        return 1
    if args.watch:
        if args.live:
            from .config import Settings
            from .hubspot import HubSpot
            from . import preflight, slack_app
            settings = Settings.load()
            results = preflight.run(settings, ["hubspot", "slack", "openai"])
            if not preflight.passed(results):
                print(preflight.render(results))
                print("\npreflight FAILED: nothing was started")
                return 1
            slack_app.start_background(settings, paths.root / "results" / "crm.sqlite",
                                       HubSpot(settings.hubspot_key, live=True))
            print("Slack handler connected: card buttons are live", flush=True)
        watch(paths, args.interval, args.force, args.run, args.live)
        return 0
    files = [Path(f).resolve() for f in args.file] if args.file else None
    results = ingest_inbox(paths, force=args.force, run=args.run, files=files, settle_seconds=0, live=args.live)
    if not results:
        print("inbox is empty")
        return 0
    for r in results:
        print(format_result(r))
    ok_blocks = sum(b.ok for r in results for b in r.blocks)
    bad_blocks = sum(not b.ok for r in results for b in r.blocks)
    bad_files = sum(not r.ok for r in results)
    print(f"\n{ok_blocks} transcript(s) ingested, {bad_blocks} failed, {bad_files} file(s) rejected")
    return 1 if bad_blocks or bad_files else 0


def cmd_request(args, paths: Paths) -> int:
    dealset = load_deals(paths)
    ids = [d.strip() for d in args.deals.split(",")] if args.deals else None
    deals = briefs.select_deals(paths, dealset, args.n, args.split, ids, args.include_existing)
    if not deals:
        print("nothing to request: every selected deal already has a transcript")
        return 0
    out = briefs.write_request(paths, dealset, deals)
    print(json.dumps({"request": out, "deals": [d["deal_id"] for d in deals]}))
    return 0


def cmd_status(args, paths: Paths) -> int:
    dealset = load_deals(paths)
    invalid = {}
    for did, d in dealset.deals.items():
        errs = validate_deal(d)
        if errs:
            invalid[did] = errs
    rows = []
    for split in SPLITS:
        ids = [d for d, x in dealset.deals.items() if x["split"] == split]
        t = sum((paths.transcripts / f"{d}.md").exists() for d in ids)
        k = sum((paths.keys / f"{d}.json").exists() for d in ids)
        a = 0
        for d in ids:
            ap = paths.audit / f"{d}.json"
            if ap.exists() and json.loads(ap.read_text()).get("verdict") == "pass":
                a += 1
        rows.append((split, len(ids), t, k, a))
    pending = len([p for p in paths.inbox.glob("*") if p.is_file() and p.suffix in (".md", ".txt")]) \
        if paths.inbox.exists() else 0
    print(f"{'split':<11}{'deals':>6}{'transcripts':>13}{'keys':>6}{'verified':>10}")
    for split, n, t, k, a in rows:
        print(f"{split:<11}{n:>6}{t:>13}{k:>6}{a:>10}")
    print(f"\ninbox pending: {pending}")
    if invalid:
        print(f"\n{len(invalid)} deal(s) fail validation:")
        for did, errs in sorted(invalid.items()):
            for e in errs:
                print(f"  {e}")
        return 1
    return 0


def cmd_slice(args, paths: Paths) -> int:
    from . import slice as slice_mod
    return slice_mod.run(paths, args.deal or "", args.live)


def cmd_eval(args, paths: Paths) -> int:
    from . import bakeoff
    from .config import Settings
    settings = Settings.load()
    dealset = load_deals(paths)
    deal_path = paths.root / "results" / "bakeoff_deals.json"
    if deal_path.exists():
        ids = json.loads(deal_path.read_text())["deals"]
    else:
        ids = bakeoff.select_deals(dealset)
        deal_path.parent.mkdir(exist_ok=True)
        deal_path.write_text(json.dumps({"seed": bakeoff.SEED, "split": "validation", "deals": ids}, indent=2))
    if args.what in ("noise", "learn", "learn-errors", "baseline"):
        from . import evalcmds
        if args.what == "noise":
            out = evalcmds.noise(settings, paths, args.run)
        elif args.what == "learn":
            out = evalcmds.learn_round(settings, paths, Path(args.prompt), args.round, args.run)
        elif args.what == "learn-errors":
            out = evalcmds.learn_errors(paths, Path(args.prompt), args.limit)
        else:
            out = evalcmds.baseline(settings, paths, args.run, args.approved)
        print(json.dumps(out, indent=2))
        return 0
    if args.what == "bakeoff-deals":
        print(json.dumps(ids))
        return 0
    missing = settings.missing("openai_api_key", "extractor_model", "learner_model")
    if missing and (args.run or args.smoke):
        print(f"missing in .env: {', '.join(missing)}")
        return 1
    if args.smoke:
        from . import db
        conn = db.connect(paths.root / "results" / "crm.sqlite")
        fixture = paths.root / "tests" / "fixtures" / "chat_reply_d002.md"
        print(json.dumps(bakeoff.smoke(settings, conn, paths, fixture), indent=2))
        return 0
    have = [d for d in ids if (paths.transcripts / f"{d}.md").exists() and (paths.keys / f"{d}.json").exists()]
    if not args.run:
        est = bakeoff.estimate(settings, paths, have, args.repeats)
        print(json.dumps({"deals_selected": len(ids), "deals_with_transcripts": len(have), **est}, indent=2))
        return 0
    if len(have) != len(ids):
        print(f"{len(ids) - len(have)} of {len(ids)} selected transcripts are missing; generate and ingest them first")
        return 1
    result = bakeoff.run_bakeoff(settings, paths, ids, args.repeats)
    print(json.dumps(result["selection"], indent=2))
    print("wrote results/bakeoff.json")
    return 0


def cmd_preflight(args, paths: Paths) -> int:
    from . import preflight
    from .config import Settings
    groups = args.only or list(preflight.GROUPS)
    results = preflight.run(Settings.load(), groups)
    print(preflight.render(results))
    ok = preflight.passed(results)
    print("\npreflight " + ("passed" if ok else "FAILED: fix the items marked FAIL before any live step"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m crm")
    p.add_argument("--root", help="repo root (default: CRM_ROOT or current directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="ingest transcript files from inbox/")
    pi.add_argument("--watch", action="store_true", help="keep polling inbox/")
    pi.add_argument("--interval", type=float, default=2.0)
    pi.add_argument("--run", action="store_true", help="send each ingested demo call to the pipeline (dry-run unless --live)")
    pi.add_argument("--live", action="store_true", help="with --run: post cards to Slack and write to HubSpot")
    pi.add_argument("--force", action="store_true", help="replace an existing transcript")
    pi.add_argument("--file", action="append", help="ingest this file instead of inbox/ (not moved)")

    pr = sub.add_parser("request", help="render a transcript request for the next deals")
    pr.add_argument("--n", type=int, default=5)
    pr.add_argument("--split", choices=SPLITS)
    pr.add_argument("--deals", help="comma-separated deal ids")
    pr.add_argument("--include-existing", action="store_true")

    sub.add_parser("status", help="dataset progress by split")

    pe = sub.add_parser("eval", help="eval experiments (dry-run plan by default)")
    pe.add_argument("what", choices=["bakeoff", "bakeoff-deals", "noise", "learn", "learn-errors", "baseline"])
    pe.add_argument("--prompt", default="prompts/extractor_v0.md", help="prompt for learn / learn-errors")
    pe.add_argument("--round", type=int, default=0, help="iteration number for `learn`")
    pe.add_argument("--limit", type=int, default=40, help="max rows for learn-errors")
    pe.add_argument("--approved", action="store_true", help="baseline prompt approved by the project owner")
    pe.add_argument("--run", action="store_true", help="call OpenAI for real (default prints the plan and estimate)")
    pe.add_argument("--smoke", action="store_true", help="one call per setting on a fixture transcript")
    pe.add_argument("--repeats", type=int, default=3)

    pp = sub.add_parser("preflight", help="verify keys, scopes, models and channels before any live step")
    pp.add_argument("--only", action="append", choices=["hubspot", "slack", "openai"])

    ps = sub.add_parser("slice", help="M0 vertical slice: one hardcoded proposal through review (dry-run by default)")
    ps.add_argument("--deal", help="deal id (default: first seeded deal)")
    ps.add_argument("--live", action="store_true", help="post to Slack and write to HubSpot")

    args = p.parse_args(argv)
    paths = Paths.from_env(args.root)
    load_env(paths.root)
    handlers = {"ingest": cmd_ingest, "request": cmd_request, "status": cmd_status, "slice": cmd_slice, "eval": cmd_eval, "preflight": cmd_preflight}
    return handlers[args.cmd](args, paths)


if __name__ == "__main__":
    sys.exit(main())
