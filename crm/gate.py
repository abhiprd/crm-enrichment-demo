"""Validation gate: does a candidate rule help, beyond noise, on the validation split?

Replays the validation transcripts REPEATS times with the current rules and with the candidate added, then
compares per-instance majority outcomes. Thresholds are fixed here, before any rule is judged:

  target field:  improved - worsened >= MIN_NET_GAIN  AND  > that field's flip allowance
  other fields:  no field may lose more than MAX_OTHER_LOSS net instances

A field's flip allowance is its V0 run-to-run flip rate x the number of transcripts. Outcomes are proposal-level
(did the extraction lead to the expected proposal, PLAN D1), so the allowance is V0's proposal-level flip rate
(results/proposal_scores.json, from `crm eval rescore`); the truth-level rate in results/v0_noise.json applies
only to metric "fields". The thresholds below did not change; only the outcome they are applied to did.
Keys are read in code only; this module writes aggregate counts, never truth."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from . import evalrun, rules
from .config import Settings
from .evalcmds import split_ids
from .extractor import compose_template, load_template
from .paths import Paths
from .schema import FIELDS

REPEATS = 3
MIN_NET_GAIN = 3
MAX_OTHER_LOSS = 2


def field_allowance(paths: Paths, n_transcripts: int, metric: str = "fields") -> dict:
    """Expected flipped instances per field: V0 flip rate x transcripts. Missing noise file means no allowance data."""
    name = "v0_noise.json" if metric == "fields" else "proposal_scores.json"
    f = paths.root / "results" / name
    if not f.exists():
        raise SystemExit(f"results/{name} is missing: the gate needs the measured noise floor"
                         + (" (run `crm eval rescore`)" if metric != "fields" else ""))
    data = json.loads(f.read_text())
    rates = data["report"]["flip_rate_by_field"] if metric == "fields" else data["v0"]["flip_rate_by_field"]
    return {fld: (rates.get(fld) or 0.0) * n_transcripts for fld in FIELDS}


def majority_correct(runs: list) -> dict:
    """{(deal, field): correct in a majority of runs}."""
    outcomes = evalrun.instance_outcomes(runs)
    need = math.ceil(len(outcomes) / 2) if len(outcomes) % 2 == 0 else len(outcomes) // 2 + 1
    keys = set.intersection(*[set(o) for o in outcomes]) if outcomes else set()
    return {k: sum(o[k] for o in outcomes) >= need for k in keys}


def net_changes(base_runs: list, cand_runs: list) -> dict:
    """Per field: {improved, worsened} instances comparing majority outcomes of the candidate to the base."""
    b, c = majority_correct(base_runs), majority_correct(cand_runs)
    out = {f: {"improved": 0, "worsened": 0} for f in FIELDS}
    for (deal, f) in set(b) & set(c):
        out[f]["improved"] += (not b[(deal, f)]) and c[(deal, f)]
        out[f]["worsened"] += b[(deal, f)] and not c[(deal, f)]
    return out


def decide(changes: dict, target: str, allowance: dict) -> dict:
    """Apply the fixed thresholds. Returns {passed, net_by_field, reasons}."""
    net = {f: changes[f]["improved"] - changes[f]["worsened"] for f in FIELDS}
    reasons = []
    if net[target] < MIN_NET_GAIN:
        reasons.append(f"{target} net gain {net[target]} is below the minimum {MIN_NET_GAIN}")
    if net[target] <= allowance[target]:
        reasons.append(f"{target} net gain {net[target]} does not exceed its noise allowance {allowance[target]:.1f}")
    for f in FIELDS:
        if f != target and -net[f] > MAX_OTHER_LOSS:
            reasons.append(f"{f} lost {-net[f]} net instances (limit {MAX_OTHER_LOSS})")
    return {"passed": not reasons, "net_by_field": net, "reasons": reasons}


def _templates(conn, rule) -> tuple:
    base_rules = [r for r in rules.active_rules(conn) if r["rule_id"] != rule["rule_id"]]
    examples = {f: rules.examples_for(conn, f) for f in FIELDS}
    base = compose_template(load_template(), base_rules, examples)
    cand = compose_template(load_template(), base_rules + [rule], examples)
    return base, cand


def plan(settings: Settings, conn, paths: Paths, rule_id: int) -> dict:
    ids = split_ids(paths, "validation")
    rule = rules.get_rule(conn, rule_id)
    return {"rule_id": rule_id, "field": rule["field"], "transcripts": len(ids), "repeats": REPEATS,
            "calls_if_nothing_cached": 2 * REPEATS * len(ids)}


def validate_rule(settings: Settings, conn, paths: Paths, rule_id: int, client=None, metric: str = "pfields",
                  out_name: Optional[str] = None) -> dict:
    """Replay validation with and without the rule; write results/<out_name or gate_<rule_id>.json>; return the
    result. `metric` is "pfields" (proposal-level, the default since M5) or "fields" (truth-level, M4)."""
    rule = rules.get_rule(conn, rule_id)
    ids = split_ids(paths, "validation")
    base_t, cand_t = _templates(conn, rule)
    cache = paths.root / "results" / "gate_runs.json"
    common = dict(model=settings.extractor_model, effort=settings.extractor_effort or None, repeats=REPEATS,
                  cache_path=cache, client=client)
    base_runs = evalrun.run_spec(settings, paths, ids, template=base_t, tag=f"gate-base-{rule_id}", **common)
    cand_runs = evalrun.run_spec(settings, paths, ids, template=cand_t, tag=f"gate-rule-{rule_id}", **common)
    base_p, cand_p = evalrun.as_metric(base_runs, metric), evalrun.as_metric(cand_runs, metric)
    changes = net_changes(base_p, cand_p)
    allowance = field_allowance(paths, len(ids), metric)
    verdict = decide(changes, rule["field"], allowance)
    before = evalrun.aggregate("base", base_p)["mean_score"]
    after = evalrun.aggregate("candidate", cand_p)["mean_score"]
    result = {"rule_id": rule_id, "field": rule["field"], "rule_text": rule["rule_text"], "metric": metric,
              "transcripts": len(ids), "repeats": REPEATS,
              "thresholds": {"min_net_gain": MIN_NET_GAIN, "max_other_loss": MAX_OTHER_LOSS},
              "allowance": {f: round(v, 2) for f, v in allowance.items()},
              "changes": changes, "truth_changes": net_changes(base_runs, cand_runs),
              "validation_score_before": before, "validation_score_after": after,
              "cost_usd": evalrun.aggregate("b", base_runs)["total_cost"] + evalrun.aggregate("c", cand_runs)["total_cost"],
              "call_errors": evalrun.aggregate("b", base_runs)["call_errors"] + evalrun.aggregate("c", cand_runs)["call_errors"],
              **verdict}
    out_path = paths.root / "results" / (out_name or f"gate_{rule_id}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def apply_verdict(conn, rule_id: int, result: dict, author: str = "gate") -> str:
    """Record the verdict. A passing rule is marked validated (and activated if it was still a candidate);
    a failing rule that is already active (demo mode) is retired in a new version. Returns a one-line summary."""
    rule = rules.get_rule(conn, rule_id)
    net = result["net_by_field"][rule["field"]]
    rules.record_validation(conn, rule_id, result["passed"], {k: result[k] for k in ("net_by_field", "reasons")},
                            result["validation_score_before"], result["validation_score_after"])
    if result["passed"]:
        if rule["status"] == "candidate":
            rules.activate(conn, rule_id, author, "passed the validation gate", validated=True)
        else:
            conn.execute("UPDATE rules SET validated=1 WHERE rule_id=?", (rule_id,))
            conn.commit()
        return f"validated: net {net:+d} on {rule['field']}"
    if rule["status"] == "active":
        rules.retire(conn, rule_id, author, "failed the validation gate: " + "; ".join(result["reasons"]))
        return "retired automatically: " + "; ".join(result["reasons"])
    return "stays a candidate: " + "; ".join(result["reasons"])
