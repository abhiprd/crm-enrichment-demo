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
                                       HubSpot(settings.hubspot_key, live=True), paths)
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
    if args.what in ("curve", "ablate", "test"):
        from . import curve
        if args.what == "curve":
            out = curve.run_curve(settings, paths) if args.run else {"dry_run": True, **curve.plan_curve(settings, paths)}
        elif args.what == "ablate":
            out = curve.run_ablation(settings, paths) if args.run else {"dry_run": True, "arms": 3, "repeats": 5}
        else:
            out = curve.run_test(settings, paths, args.confirm_frozen) if args.run else {"dry_run": True, **curve.test_plan(settings, paths)}
        print(json.dumps(out, indent=2))
        return 0
    if args.what in ("noise", "learn", "learn-errors", "baseline", "rescore"):
        from . import evalcmds
        if args.what == "rescore":
            out = evalcmds.rescore(paths)
        elif args.what == "noise":
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


def cmd_handreview(args, paths: Paths) -> int:
    from . import handreview
    from .config import Settings
    settings = Settings.load()
    if args.action == "report":
        print(json.dumps(handreview.report(paths), indent=2))
        return 0
    if args.action == "build":
        if not args.run:
            print(json.dumps({"dry_run": True, "calls": handreview.choose_calls(paths), "extractor_calls": len(handreview.choose_calls(paths))}))
            return 0
        print(json.dumps(handreview.build(settings, paths), indent=2))
        return 0
    if not args.live:
        print("post and serve talk to Slack: pass --live")
        return 1
    if args.action == "post":
        print(json.dumps({"posted": handreview.post(settings, paths)}, indent=2))
        return 0
    handreview.serve(settings, paths)
    return 0


def cmd_charts(args, paths: Paths) -> int:
    from . import charts
    from .config import Settings
    s = Settings.load()
    for f in charts.build_all(paths, s.extractor_model, s.learner_model, s.extractor_effort or None):
        print("wrote", f)
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


def cmd_rules(args, paths: Paths) -> int:
    from . import db, gate, rules
    from .config import Settings
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    cur = rules.current_version(conn)
    if args.action == "list":
        print(f"current ruleset: v{cur['version_id']} ({cur['change_type']}): {cur['reason']}")
        for r in conn.execute("SELECT * FROM rules ORDER BY rule_id"):
            flag = {None: "unvalidated", 0: "failed validation", 1: "validated"}[r["validated"]]
            print(f"  #{r['rule_id']} [{r['status']}, {flag}] {r['field']}: {r['rule_text']}")
        for v in conn.execute("SELECT * FROM ruleset_versions ORDER BY version_id"):
            print(f"  v{v['version_id']} {v['change_type']} rules={v['active_rule_ids']} {v['reason']}")
        return 0
    if args.action == "show":
        r = rules.get_rule(conn, args.id)
        if r is None:
            print(f"no rule {args.id}")
            return 1
        print(json.dumps({k: r[k] for k in r.keys()}, indent=2))
        return 0
    if args.action == "revert":
        v = rules.revert(conn, args.id, "cli", "reverted from the command line")
        print(f"reverted to the rules of v{args.id} as new version v{v}" if v else f"no version {args.id}")
        return 0 if v else 1
    settings = Settings.load()
    if rules.get_rule(conn, args.id) is None:
        print(f"no rule {args.id}")
        return 1
    if not args.run:
        print(json.dumps({"dry_run": True, **gate.plan(settings, conn, paths, args.id)}, indent=2))
        return 0
    result = gate.validate_rule(settings, conn, paths, args.id)
    print(gate.apply_verdict(conn, args.id, result))
    return 0


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
    pe.add_argument("what", choices=["bakeoff", "bakeoff-deals", "noise", "learn", "learn-errors", "baseline", "rescore", "curve", "ablate", "test"])
    pe.add_argument("--prompt", default="prompts/extractor_v0.md", help="prompt for learn / learn-errors")
    pe.add_argument("--round", type=int, default=0, help="iteration number for `learn`")
    pe.add_argument("--limit", type=int, default=40, help="max rows for learn-errors")
    pe.add_argument("--approved", action="store_true", help="baseline prompt approved by the project owner")
    pe.add_argument("--confirm-frozen", action="store_true", help="test: the project owner confirmed the final version is frozen")
    pe.add_argument("--run", action="store_true", help="call OpenAI for real (default prints the plan and estimate)")
    pe.add_argument("--smoke", action="store_true", help="one call per setting on a fixture transcript")
    pe.add_argument("--repeats", type=int, default=3)

    ph = sub.add_parser("handreview", help="hand-review cards for ~10 validation calls (no HubSpot writes)")
    ph.add_argument("action", choices=["build", "post", "serve", "report"])
    ph.add_argument("--run", action="store_true", help="build: call OpenAI (default prints the plan)")
    ph.add_argument("--live", action="store_true", help="post/serve: use Slack")

    sub.add_parser("charts", help="write docs/charts/*.svg and results/unit_economics.json from results/")

    pp = sub.add_parser("preflight", help="verify keys, scopes, models and channels before any live step")
    pp.add_argument("--only", action="append", choices=["hubspot", "slack", "openai"])

    pr2 = sub.add_parser("rules", help="learned rules: list, show, revert, validate")
    pr2.add_argument("action", choices=["list", "show", "revert", "validate"])
    pr2.add_argument("id", nargs="?", type=int, help="rule id (show, validate) or version id (revert)")
    pr2.add_argument("--run", action="store_true", help="validate: call the model (default prints the plan)")

    ps = sub.add_parser("slice", help="M0 vertical slice: one hardcoded proposal through review (dry-run by default)")
    ps.add_argument("--deal", help="deal id (default: first seeded deal)")
    ps.add_argument("--live", action="store_true", help="post to Slack and write to HubSpot")

    args = p.parse_args(argv)
    paths = Paths.from_env(args.root)
    load_env(paths.root)
    handlers = {"ingest": cmd_ingest, "request": cmd_request, "status": cmd_status, "slice": cmd_slice, "eval": cmd_eval, "preflight": cmd_preflight, "handreview": cmd_handreview, "charts": cmd_charts, "rules": cmd_rules}
    return handlers[args.cmd](args, paths)


if __name__ == "__main__":
    sys.exit(main())
