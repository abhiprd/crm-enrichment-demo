"""M5 experiments: the learning curve (oracle reviewer through the learn split in batches, gated rules, validation
scored after each version), the ablation (rules only, few-shot only, both), and the single test run.

All scoring here is proposal-level (extraction -> proposal vs `expected_proposal`, PLAN D1); truth-level means are
reported alongside. Rules and edits live in results/curve.sqlite, a database separate from the live demo's
crm.sqlite, so the curve starts from the empty ruleset. Isolation: learn-split keys are read in code to play the
oracle; validation and test keys are read in code only to score. Output files hold aggregates, never truth."""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

from . import db, evalrun, gate, learner, oracle, rules, stats
from .config import Settings
from .deals import load_deals
from .evalcmds import BASELINE_PROMPT, REPEATS, V0_PROMPT, split_ids
from .extractor import compose_template, extract, load_template, prompt_version
from .ingest import parse_transcript
from .paths import Paths
from .schema import FIELDS, HOUSE_RULES, TRAPS

BATCH_SIZE = 5
MAX_CANDIDATES_PER_BATCH = 3
SNAPSHOT_REPEATS = 3
ORDER_SEED = 1337
CURVE_DB = "curve.sqlite"


# ---- state and templates ----------------------------------------------------------------------------

def curve_db(paths: Paths, fresh: bool = False) -> sqlite3.Connection:
    path = paths.root / "results" / CURVE_DB
    if fresh and path.exists():
        path.unlink()
    return db.connect(path)


def current_template(conn: sqlite3.Connection, with_rules: bool = True, with_examples: bool = True,
                     base: Optional[str] = None) -> str:
    return compose_template(base if base is not None else load_template(),
                            rules.active_rules(conn) if with_rules else [],
                            {f: rules.examples_for(conn, f) for f in FIELDS} if with_examples else {})


def batches(paths: Paths) -> list:
    ids = split_ids(paths, "learn")
    random.Random(ORDER_SEED).shuffle(ids)
    return [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]


# ---- grouping validation instances ------------------------------------------------------------------

def instance_groups(paths: Paths, ids: list) -> dict:
    """{(deal, field): {"group": house_rule|trap|other, "cases": [case ids]}} from the case labels in deals.json.
    house_rule outranks trap when an instance carries both."""
    deals = load_deals(paths).deals
    out = {(d, f): {"group": "other", "cases": []} for d in ids for f in FIELDS}
    for d in ids:
        for c in deals[d]["cases"]:
            cell = out[(d, c["field"])]
            cell["cases"].append(c["id"])
            if c["id"] in HOUSE_RULES:
                cell["group"] = "house_rule"
            elif c["id"] in TRAPS and cell["group"] != "house_rule":
                cell["group"] = "trap"
    return out


def breakdown(runs: list, groups: dict) -> dict:
    """Mean proposal-level credit per group and per case id, pooled over runs and instances."""
    by_group: dict = {}
    by_case: dict = {}
    for run in runs:
        for r in run:
            if "error" in r:
                continue
            for f in FIELDS:
                cell = groups.get((r["deal_id"], f))
                if cell is None:
                    continue
                s = r["fields"][f]["score"]
                by_group.setdefault(cell["group"], []).append(s)
                for c in cell["cases"]:
                    by_case.setdefault(c, []).append(s)
    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    n_runs = max(len(runs), 1)
    return {"by_group": {g: {"instances": len(v) // n_runs, "mean": mean(v)} for g, v in sorted(by_group.items())},
            "by_case": {c: {"instances": len(v) // n_runs, "mean": mean(v)} for c, v in sorted(by_case.items())}}


def _score_arm(settings: Settings, paths: Paths, ids: list, template: str, repeats: int, cache: str, tag: str,
               groups: dict, client=None) -> tuple:
    """Run a template over `ids`; returns (raw runs, summary dict at proposal level with truth means alongside)."""
    runs = evalrun.run_spec(settings, paths, ids, model=settings.extractor_model, effort=settings.extractor_effort or None,
                            template=template, repeats=repeats, cache_path=paths.root / "results" / cache, tag=tag,
                            client=client)
    prop = evalrun.as_metric(runs, "pfields")
    agg, truth = evalrun.aggregate(tag, prop), evalrun.aggregate(tag, runs)
    summary = {"prompt_version": prompt_version(template), "repeats": repeats, "calls": agg["calls"],
               "call_errors": agg["call_errors"], "run_scores": agg["run_scores"], "mean_score": agg["mean_score"],
               "per_field_mean": agg["per_field_mean"], "truth_mean_score": truth["mean_score"],
               "truth_per_field_mean": truth["per_field_mean"], "unsupported": agg["unsupported"],
               "extracted_fields": agg["extracted_fields"], "cost_usd": agg["total_cost"],
               **breakdown(prop, groups)}
    return runs, summary


# ---- the learning curve -----------------------------------------------------------------------------

def pick_candidates(rejects: list, limit: int = MAX_CANDIDATES_PER_BATCH) -> list:
    """One reject per field, the fields with the most rejects first (ties in schema order), at most `limit`."""
    count = Counter(r["field"] for r in rejects)
    first: dict = {}
    for r in rejects:
        first.setdefault(r["field"], r)
    order = sorted(first, key=lambda f: (-count[f], FIELDS.index(f)))
    return [first[f] for f in order[:limit]]


def review_batch(settings: Settings, conn: sqlite3.Connection, paths: Paths, ids: list, template: str, version: int,
                 client=None) -> dict:
    """Extract each call in the batch under `template` and let the oracle review every proposal."""
    deals = load_deals(paths).deals
    decisions, cost = [], 0.0
    for d in ids:
        block = parse_transcript((paths.transcripts / f"{d}.md").read_text(encoding="utf-8"))
        key = json.loads((paths.keys / f"{d}.json").read_text(encoding="utf-8"))
        ex = extract(settings, conn, block, model=settings.extractor_model, effort=settings.extractor_effort or None,
                     run_id=f"curve-review-{d}", client=client, template=template)
        cost += ex.llm.cost_usd + ex.extra_cost
        res = oracle.review_extraction(conn, paths, d, block, ex, key, deals[d]["cases"], version, settings.extractor_model)
        decisions += [{"deal": d, **x} for x in res["decisions"]]
    return {"decisions": decisions, "cost_usd": cost}


def summarize_reviews(decisions: list) -> dict:
    by_field: dict = {}
    for x in decisions:
        by_field.setdefault(x["field"], Counter())[x["kind"]] += 1
    return {"reviewed": len(decisions), "approved": sum(x["kind"] == "approve" for x in decisions),
            "edited": sum(x["kind"] == "edit" for x in decisions), "rejected": sum(x["kind"] == "reject" for x in decisions),
            "by_field": {f: dict(c) for f, c in sorted(by_field.items())},
            "reject_reasons": dict(Counter(x["reason"] for x in decisions if x["kind"] == "reject"))}


def learn_batch(settings: Settings, conn: sqlite3.Connection, paths: Paths, decisions: list, batch_no: int,
                gate_client=None, learner_client=None) -> list:
    """Candidate rules from this batch's rejects, each gated on validation (rules activate only after passing)."""
    rejects = [x for x in decisions if x["kind"] == "reject"]
    events = []
    for r in pick_candidates(rejects):
        res = learner.learn_from_reject(settings, conn, r["proposal_id"], client=learner_client)
        ev = {"batch": batch_no, "field": r["field"], "source_deal": r["deal"], "reason": r["reason"],
              "status": res.status, "skip_reason": res.reason}
        if res.status == "created":
            result = gate.validate_rule(settings, conn, paths, res.rule_id, client=gate_client, metric="pfields",
                                        out_name=f"curve_gates/gate_{res.rule_id}.json")
            summary = gate.apply_verdict(conn, res.rule_id, result, author="curve-gate")
            ev.update({"rule_id": res.rule_id, "rule_text": res.rule_text, "gate_passed": result["passed"],
                       "net_by_field": result["net_by_field"], "gate_reasons": result["reasons"], "summary": summary,
                       "gate_cost_usd": result["cost_usd"]})
        events.append(ev)
    return events


def learner_spend(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE purpose='rule-learner'").fetchone()
    return float(row[0])


def plan_curve(settings: Settings, paths: Paths) -> dict:
    n_learn, n_val = len(split_ids(paths, "learn")), len(split_ids(paths, "validation"))
    n_batches = len(batches(paths))
    gate_calls = n_batches * MAX_CANDIDATES_PER_BATCH * 2 * gate.REPEATS * n_val
    snap_calls = (n_batches + 1) * SNAPSHOT_REPEATS * n_val
    calls = n_learn + gate_calls + snap_calls
    return {"learn_transcripts": n_learn, "batches": n_batches, "validation_transcripts": n_val,
            "review_calls": n_learn, "snapshot_calls_max": snap_calls, "gate_calls_max": gate_calls,
            "calls_max": calls, "note": "upper bound; cached prompts and batches without a candidate cost nothing; "
                                        "estimate at ~$0.0005 per call (results/bakeoff.json)"}


def run_curve(settings: Settings, paths: Paths, client=None, gate_client=None, learner_client=None) -> dict:
    """Fresh curve.sqlite; point 0 is the empty ruleset, then one point per batch. Writes results/curve.json."""
    conn = curve_db(paths, fresh=True)
    val_ids = split_ids(paths, "validation")
    groups = instance_groups(paths, val_ids)
    points, reviews, rule_events = [], [], []
    base_template = load_template(paths.root / V0_PROMPT)
    version = rules.current_version(conn)["version_id"]
    _, snap = _score_arm(settings, paths, val_ids, current_template(conn, base=base_template), SNAPSHOT_REPEATS,
                         "curve_runs.json", "curve-snap-0", groups, client)
    points.append({"batch": 0, "ruleset_version": version, "active_rule_ids": [], "examples": 0, **snap})
    spend = {"review": 0.0, "gates": 0.0, "snapshots": snap["cost_usd"]}
    for b, ids in enumerate(batches(paths), start=1):
        version = rules.current_version(conn)["version_id"]
        template = current_template(conn, base=base_template)
        rv = review_batch(settings, conn, paths, ids, template, version, client)
        spend["review"] += rv["cost_usd"]
        reviews.append({"batch": b, "deals": ids, "ruleset_version_before": version, **summarize_reviews(rv["decisions"])})
        events = learn_batch(settings, conn, paths, rv["decisions"], b, gate_client, learner_client)
        rule_events += events
        spend["gates"] += sum(e.get("gate_cost_usd", 0.0) for e in events)
        version = rules.current_version(conn)["version_id"]
        template = current_template(conn, base=base_template)
        _, snap = _score_arm(settings, paths, val_ids, template, SNAPSHOT_REPEATS, "curve_runs.json",
                             f"curve-snap-{b}", groups, client)
        spend["snapshots"] += snap["cost_usd"]
        n_ex = sum(len(rules.examples_for(conn, f)) for f in FIELDS)
        points.append({"batch": b, "ruleset_version": version, "active_rule_ids": [r["rule_id"] for r in rules.active_rules(conn)],
                       "examples": n_ex, **snap})
    spend["learner"] = learner_spend(conn)
    final_rules = [{"rule_id": r["rule_id"], "field": r["field"], "rule_text": r["rule_text"],
                    "validated": r["validated"]} for r in rules.active_rules(conn)]
    final_examples = {f: rules.examples_for(conn, f) for f in FIELDS if rules.examples_for(conn, f)}
    final_tpl = current_template(conn, base=base_template)
    out = {"metric": "proposal-level (PLAN D1); truth_* fields are the truth-level scores of the same runs",
           "config": {"batch_size": BATCH_SIZE, "max_candidates_per_batch": MAX_CANDIDATES_PER_BATCH,
                      "snapshot_repeats": SNAPSHOT_REPEATS, "order_seed": ORDER_SEED, "rules_mode": "gated",
                      "gate": {"repeats": gate.REPEATS, "min_net_gain": gate.MIN_NET_GAIN,
                               "max_other_loss": gate.MAX_OTHER_LOSS}, "start_prompt": str(V0_PROMPT)},
           "points": points, "reviews": reviews, "rule_events": rule_events,
           "final": {"ruleset_version": version, "rules": final_rules, "example_counts": {f: len(v) for f, v in final_examples.items()},
                     "prompt_version": prompt_version(final_tpl)},
           "spend_usd": {k: round(v, 4) for k, v in spend.items()}, "spend_total_usd": round(sum(spend.values()), 4)}
    (paths.root / "results" / "curve.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


# ---- ablation and test run --------------------------------------------------------------------------

def _arms(conn: sqlite3.Connection, base: str) -> dict:
    return {"rules_only": current_template(conn, True, False, base), "fewshot_only": current_template(conn, False, True, base),
            "both": current_template(conn, True, True, base)}


def _v0_proposal_runs(paths: Paths, split_ids_: list) -> tuple:
    v0 = json.loads((paths.root / "results" / "v0_noise.json").read_text())["report"]
    cache = json.loads((paths.root / "results" / "v0_noise_runs.json").read_text())
    runs = [[evalrun.rescore_proposals(paths, cache[evalrun.cache_key(v0["prompt_version"], v0["model"], v0["effort"], rep, d)])
             for d in split_ids_] for rep in range(v0["repeats"])]
    return v0, runs


def _compare(base_runs: list, cand_runs: list) -> dict:
    """Paired comparison at proposal level; the noise bar is the instances that flip between `base_runs` themselves."""
    b, c = evalrun.as_metric(base_runs, "pfields"), evalrun.as_metric(cand_runs, "pfields")
    flips = stats.flip_instances(evalrun.instance_outcomes(b))
    return stats.compare(evalrun.instance_credits(b), evalrun.instance_credits(c), flips)


def run_ablation(settings: Settings, paths: Paths, client=None) -> dict:
    """Rules only, few-shot only, both, on validation, 5 runs each -> results/ablation.json. Uses the final state of
    results/curve.sqlite. V0 is the saved 5-run noise run."""
    conn = curve_db(paths)
    val_ids = split_ids(paths, "validation")
    groups = instance_groups(paths, val_ids)
    v0, v0_runs = _v0_proposal_runs(paths, val_ids)
    base = load_template(paths.root / V0_PROMPT)
    out = {"metric": "proposal-level (PLAN D1)", "repeats": REPEATS, "arms": {}}
    out["v0"] = {"run_scores": [r["mean_score"] for r in (evalrun.aggregate("v0", [x]) for x in evalrun.as_metric(v0_runs, "pfields"))],
                 **breakdown(evalrun.as_metric(v0_runs, "pfields"), groups)}
    out["v0"]["mean_score"] = sum(out["v0"]["run_scores"]) / len(out["v0"]["run_scores"])
    for name, tpl in _arms(conn, base).items():
        runs, summ = _score_arm(settings, paths, val_ids, tpl, REPEATS, "ablation_runs.json", f"ablate-{name}", groups, client)
        out["arms"][name] = {**summ, "paired_vs_v0": _compare(v0_runs, runs),
                             "rules": [r["rule_id"] for r in rules.active_rules(conn)] if name != "fewshot_only" else [],
                             "examples": sum(len(rules.examples_for(conn, f)) for f in FIELDS) if name != "rules_only" else 0}
    out["spend_usd"] = round(sum(a["cost_usd"] for a in out["arms"].values()), 4)
    (paths.root / "results" / "ablation.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def test_plan(settings: Settings, paths: Paths) -> dict:
    n = len(split_ids(paths, "test"))
    return {"transcripts": n, "arms": ["v0", "manual_baseline", "final"], "repeats": REPEATS, "calls": 3 * REPEATS * n,
            "final_prompt_version": json.loads((paths.root / "results" / "curve.json").read_text())["final"]["prompt_version"]
            if (paths.root / "results" / "curve.json").exists() else None}


def run_test(settings: Settings, paths: Paths, confirmed_frozen: bool, client=None) -> dict:
    """The single final run on the test split: V0, manual baseline, and the final version, 5 runs each ->
    results/test_run.json. Refuses to run twice or without confirmation that the final version is frozen."""
    out_path = paths.root / "results" / "test_run.json"
    if out_path.exists():
        raise SystemExit("results/test_run.json exists: the test set has already been run; ask the project owner")
    if not confirmed_frozen:
        raise SystemExit("the final version must be confirmed frozen by the project owner: pass --confirm-frozen")
    curve_path = paths.root / "results" / "curve.json"
    if not curve_path.exists():
        raise SystemExit("results/curve.json is missing: run the curve first")
    conn = curve_db(paths)
    base = load_template(paths.root / V0_PROMPT)
    final_tpl = current_template(conn, base=base)
    expected = json.loads(curve_path.read_text())["final"]["prompt_version"]
    if prompt_version(final_tpl) != expected:
        raise SystemExit(f"the final version changed since the curve ({prompt_version(final_tpl)} != {expected})")
    ids = split_ids(paths, "test")
    groups = instance_groups(paths, ids)
    arms = {"v0": base, "manual_baseline": (paths.root / BASELINE_PROMPT).read_text(encoding="utf-8"), "final": final_tpl}
    runs_by_arm, summaries = {}, {}
    for name, tpl in arms.items():
        runs_by_arm[name], summaries[name] = _score_arm(settings, paths, ids, tpl, REPEATS, "test_runs.json",
                                                         f"test-{name}", groups, client)
    out = {"metric": "proposal-level (PLAN D1)", "split": "test", "transcripts": len(ids), "repeats": REPEATS,
           "arms": summaries,
           "paired": {"baseline_vs_v0": _compare(runs_by_arm["v0"], runs_by_arm["manual_baseline"]),
                      "final_vs_v0": _compare(runs_by_arm["v0"], runs_by_arm["final"]),
                      "final_vs_baseline": _compare(runs_by_arm["manual_baseline"], runs_by_arm["final"])},
           "note": "noise bar for each pairing = instances that flip between the first-named arm's own runs; "
                   "the V0 bar is used for baseline_vs_v0 and final_vs_v0, the baseline's own for final_vs_baseline",
           "spend_usd": round(sum(s["cost_usd"] for s in summaries.values()), 4)}
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out
