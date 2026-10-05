"""`crm eval noise | learn | learn-errors | baseline`: the M2 experiments. Dry-run plan by default.

Isolation: noise and baseline runs read validation keys in code only. `learn` and `learn-errors` touch the
learn split only, which is the one split the main session may inspect while tuning prompts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import evalrun, stats
from .config import Settings
from .deals import load_deals
from .extractor import build_prompt, load_template, prompt_version
from .ingest import parse_transcript
from .paths import Paths
from .schema import FIELDS

V0_PROMPT = Path("prompts/extractor_v0.md")
BASELINE_PROMPT = Path("prompts/extractor_baseline.md")
REPEATS = 5


def split_ids(paths: Paths, split: str) -> list:
    deals = load_deals(paths).deals
    return sorted(d for d, x in deals.items() if x["split"] == split
                  and (paths.transcripts / f"{d}.md").exists() and (paths.keys / f"{d}.json").exists())


def _write(paths: Paths, name: str, data: dict) -> None:
    (paths.root / "results").mkdir(exist_ok=True)
    (paths.root / "results" / name).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _plan(settings: Settings, paths: Paths, ids: list, template: str, repeats: int) -> dict:
    toks = [len(build_prompt(parse_transcript((paths.transcripts / f"{d}.md").read_text(encoding="utf-8")),
                             template)) / 4 for d in ids]
    avg = sum(toks) / len(toks) if toks else 0.0
    calls = repeats * len(ids)
    return {"model": settings.extractor_model, "effort": settings.extractor_effort or "default",
            "prompt_version": prompt_version(template), "transcripts": len(ids), "repeats": repeats,
            "calls": calls, "avg_input_tokens": round(avg),
            "estimated_cost_usd": round(evalrun.estimate_cost(settings, settings.extractor_model, avg, calls), 4)}


def _effort(settings: Settings) -> Optional[str]:
    return settings.extractor_effort or None


def noise(settings: Settings, paths: Paths, run: bool, client=None) -> dict:
    """V0, pinned model/effort, 5 runs on the full validation split -> results/v0_noise.json."""
    ids, template = split_ids(paths, "validation"), load_template(paths.root / V0_PROMPT)
    plan = _plan(settings, paths, ids, template, REPEATS)
    if not run:
        return {"dry_run": True, **plan}
    runs = evalrun.run_spec(settings, paths, ids, model=settings.extractor_model, effort=_effort(settings),
                            template=template, repeats=REPEATS, cache_path=paths.root / "results" / "v0_noise_runs.json",
                            tag="v0-noise", client=client)
    report = evalrun.run_report("v0-noise", runs, model=settings.extractor_model, effort=_effort(settings),
                                prompt=prompt_version(template), split="validation")
    out = {"report": report, "bar_for_paired_tests": report["flipped_instances"],
           "note": "A change clears the noise floor only if its paired-test p < 0.05 and it changes more "
                   "instances than flipped between V0's own runs (SPEC section 6)."}
    _write(paths, "v0_noise.json", out)
    return out


def learn_round(settings: Settings, paths: Paths, prompt_path: Path, round_no: int, run: bool, client=None) -> dict:
    """One prompt-iteration round on the LEARN split (1 run). Appends to results/baseline_iterations.json."""
    ids, template = split_ids(paths, "learn"), load_template(paths.root / prompt_path)
    plan = _plan(settings, paths, ids, template, 1)
    if not run:
        return {"dry_run": True, "split": "learn", "round": round_no, **plan}
    runs = evalrun.run_spec(settings, paths, ids, model=settings.extractor_model, effort=_effort(settings),
                            template=template, repeats=1, cache_path=paths.root / "results" / "learn_runs.json",
                            tag=f"learn-r{round_no}", client=client)
    rep = evalrun.run_report(f"learn-round-{round_no}", runs, model=settings.extractor_model,
                             effort=_effort(settings), prompt=prompt_version(template), split="learn")
    path = paths.root / "results" / "baseline_iterations.json"
    hist = json.loads(path.read_text()) if path.exists() else []
    hist = [h for h in hist if h["round"] != round_no] + [
        {"round": round_no, "prompt": str(prompt_path), "prompt_version": rep["prompt_version"],
         "mean_score": rep["mean_score"], "per_field_mean": rep["per_field_mean"], "cost": rep["total_cost"],
         "calls": rep["calls"], "unsupported": rep["unsupported"]}]
    path.write_text(json.dumps(sorted(hist, key=lambda h: h["round"]), indent=2), encoding="utf-8")
    return rep


def learn_errors(paths: Paths, prompt_path: Path, limit: int = 40) -> list:
    """Learn-split mistakes for the prompt being iterated: truth vs raw extraction. Learn only, by design."""
    template = load_template(paths.root / prompt_path)
    version = prompt_version(template)
    cache = json.loads((paths.root / "results" / "learn_runs.json").read_text())
    deals = load_deals(paths).deals
    out = []
    for key, rec in sorted(cache.items()):
        if not key.startswith(version + "|") or "error" in rec:
            continue
        d = rec["deal_id"]
        if deals[d]["split"] != "learn":  # hard guard: never surface validation or test errors
            continue
        truth = json.loads((paths.keys / f"{d}.json").read_text())["fields"]
        for f in FIELDS:
            if not rec["fields"][f]["correct"]:
                out.append({"deal": d, "field": f, "score": rec["fields"][f]["score"],
                            "truth": truth[f]["truth"], "pred": {k: rec["raw"][f][k] for k in ("value", "status")},
                            "cases": [c["id"] for c in deals[d]["cases"] if c["field"] == f]})
    return out[:limit]


def baseline(settings: Settings, paths: Paths, run: bool, approved: bool, client=None) -> dict:
    """Frozen baseline prompt, 5 runs on validation, paired against V0 -> results/manual_baseline.json."""
    noise_path = paths.root / "results" / "v0_noise.json"
    prompt_file = paths.root / BASELINE_PROMPT
    if not noise_path.exists():
        raise SystemExit("results/v0_noise.json is missing: run `eval noise --run` first")
    if not prompt_file.exists():
        raise SystemExit(f"{BASELINE_PROMPT} is missing: finish the learn-split iteration first")
    ids, template = split_ids(paths, "validation"), prompt_file.read_text(encoding="utf-8")
    plan = _plan(settings, paths, ids, template, REPEATS)
    if not run:
        return {"dry_run": True, **plan}
    if not approved:
        raise SystemExit("the baseline prompt must be approved before it is scored on validation: pass --approved")
    cache = paths.root / "results" / "manual_baseline_runs.json"
    runs = evalrun.run_spec(settings, paths, ids, model=settings.extractor_model, effort=_effort(settings),
                            template=template, repeats=REPEATS, cache_path=cache, tag="baseline", client=client)
    report = evalrun.run_report("manual-baseline", runs, model=settings.extractor_model, effort=_effort(settings),
                                prompt=prompt_version(template), split="validation")
    v0 = json.loads(noise_path.read_text())["report"]
    v0_cache = json.loads((paths.root / "results" / "v0_noise_runs.json").read_text())
    v0_runs = [[v0_cache[evalrun.cache_key(v0["prompt_version"], v0["model"], v0["effort"], rep, d)]
                for d in ids] for rep in range(REPEATS)]
    cmp = stats.compare(evalrun.instance_credits(v0_runs), evalrun.instance_credits(runs), v0["flipped_instances"])
    iters = paths.root / "results" / "baseline_iterations.json"
    out = {"report": report, "v0": {"mean_score": v0["mean_score"], "run_scores": v0["run_scores"],
                                   "prompt_version": v0["prompt_version"]},
           "paired_vs_v0": cmp, "learn_iterations": json.loads(iters.read_text()) if iters.exists() else [],
           "note": "Prompt tuned on the learn split only; scored on validation, never used for tuning."}
    _write(paths, "manual_baseline.json", out)
    return out


def _cached_runs(paths: Paths, cache_name: str, report: dict, ids: list) -> list:
    cache = json.loads((paths.root / "results" / cache_name).read_text())
    return [[evalrun.rescore_proposals(paths, cache[evalrun.cache_key(report["prompt_version"], report["model"],
                                                                      report["effort"], rep, d)])
             for d in ids] for rep in range(report["repeats"])]


def rescore(paths: Paths) -> dict:
    """Proposal-level view of the saved V0 noise runs and manual-baseline runs (no API calls) ->
    results/proposal_scores.json. Includes V0's proposal-level flip rates, which the gate uses as its allowance."""
    v0 = json.loads((paths.root / "results" / "v0_noise.json").read_text())["report"]
    bl = json.loads((paths.root / "results" / "manual_baseline.json").read_text())["report"]
    ids = split_ids(paths, "validation")
    v0_runs = evalrun.as_metric(_cached_runs(paths, "v0_noise_runs.json", v0, ids), "pfields")
    bl_runs = evalrun.as_metric(_cached_runs(paths, "manual_baseline_runs.json", bl, ids), "pfields")
    rep = lambda label, runs, src: evalrun.run_report(label, runs, model=src["model"], effort=src["effort"],  # noqa: E731
                                                      prompt=src["prompt_version"], split="validation")
    r0, r1 = rep("v0-proposal", v0_runs, v0), rep("baseline-proposal", bl_runs, bl)
    cmp = stats.compare(evalrun.instance_credits(v0_runs), evalrun.instance_credits(bl_runs), r0["flipped_instances"])
    out = {"metric": "proposal-level: extraction -> proposal vs expected_proposal (PLAN D1)",
           "v0": r0, "manual_baseline": r1, "paired_baseline_vs_v0": cmp,
           "note": "Re-scored from saved raw extractions; no API calls. next_step proposes the first listed step only."}
    _write(paths, "proposal_scores.json", out)
    return out
