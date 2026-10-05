"""Shared eval machinery: run one extraction instance, aggregate runs, and run a prompt/model setting over a
split with a resumable cache. Per-instance results (which carry raw extractions) go to *_runs.json files that
the main Claude session never opens; only the aggregate JSON files are read."""

from __future__ import annotations

import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from . import db, stats
from .config import Settings
from .extractor import extract, load_template, prompt_version
from .ingest import parse_transcript
from .paths import Paths
from .schema import FIELDS
from .propscore import score_proposals
from .scoring import field_score, score_extraction
from .validator import apply_validation, validate_fields


def run_instance(settings: Settings, conn, paths: Paths, deal_id: str, model: str, effort: Optional[str],
                 run_id: str, template: Optional[str] = None, client=None) -> dict:
    """Extract one transcript, validate quotes, score against the key. The record keeps the raw extraction
    so a later scoring change can be re-scored without new API calls."""
    block = parse_transcript((paths.transcripts / f"{deal_id}.md").read_text(encoding="utf-8"))
    key = json.loads((paths.keys / f"{deal_id}.json").read_text(encoding="utf-8"))
    try:
        ex = extract(settings, conn, block, model=model, effort=effort, run_id=run_id, client=client,
                     template=template)
    except Exception as e:  # an API failure is a recorded failure, not a silent zero
        return {"deal_id": deal_id, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    utts = {u.idx: u.text for u in block.utterances}
    bad = validate_fields(ex.fields, utts)
    n_extracted = sum(f["status"] != "not_mentioned" for f in ex.fields.values())
    validated = apply_validation(ex.fields, bad)
    scores = score_extraction(key["fields"], validated)
    return {"deal_id": deal_id, "field_score": field_score(scores),
            "fields": {f: {"score": s["score"], "correct": s["correct"]} for f, s in scores.items()},
            "pfields": score_proposals(key["fields"], validated),
            "unsupported": len(bad), "extracted": n_extracted, "parse_errors": len(ex.errors), "retries": ex.retries,
            "in": ex.llm.input_tokens, "out": ex.llm.output_tokens, "reasoning": ex.llm.reasoning_tokens,
            "cost": ex.llm.cost_usd + ex.extra_cost, "ms": ex.llm.latency_ms, "raw": ex.fields, "prompt_version": ex.prompt_version}


def rescore_proposals(paths: Paths, rec: dict) -> dict:
    """Add proposal-level outcomes (`pfields`) to a record that predates them, from its saved raw extraction.
    No API call: quote validation and the key are applied again in code."""
    if "error" in rec or "pfields" in rec:
        return rec
    block = parse_transcript((paths.transcripts / f"{rec['deal_id']}.md").read_text(encoding="utf-8"))
    key = json.loads((paths.keys / f"{rec['deal_id']}.json").read_text(encoding="utf-8"))
    bad = validate_fields(rec["raw"], {u.idx: u.text for u in block.utterances})
    return {**rec, "pfields": score_proposals(key["fields"], apply_validation(rec["raw"], bad))}


def as_metric(runs: list, metric: str) -> list:
    """View runs through one metric. metric "fields" (truth scoring) returns them unchanged; "pfields"
    (proposal scoring) swaps in the proposal outcomes so aggregate, flips and paired tests work as before."""
    if metric == "fields":
        return runs
    return [[r if "error" in r else {**r, "fields": r["pfields"],
                                     "field_score": _mean([r["pfields"][f]["score"] for f in FIELDS])}
             for r in run] for run in runs]


def _mean(xs: list) -> float:
    return statistics.fmean(xs) if xs else 0.0


def aggregate(label: str, runs: list) -> dict:
    """runs: list of per-repeat lists of per-deal records (errors excluded from scoring, counted)."""
    good = [[r for r in run if "error" not in r] for run in runs]
    errors = sum(len(run) - len(g) for run, g in zip(runs, good))
    scores = [_mean([r["field_score"] for r in g]) for g in good]
    flat = [r for g in good for r in g]
    flips: dict = {}
    if len(good) > 1:
        deals = set.intersection(*[{r["deal_id"] for r in g} for g in good])
        for f in FIELDS:
            n_flip = 0
            for d in deals:
                outcomes = {next(r for r in g if r["deal_id"] == d)["fields"][f]["correct"] for g in good}
                n_flip += len(outcomes) > 1
            flips[f] = n_flip / len(deals) if deals else None
    per_field = {f: _mean([r["fields"][f]["score"] for r in flat]) for f in FIELDS} if flat else {}
    extracted = sum(r["extracted"] for r in flat)
    return {
        "setting": label, "repeats": len(runs), "transcripts_per_run": len(runs[0]) if runs else 0,
        "calls": sum(len(r) for r in runs), "call_errors": errors,
        "run_scores": scores, "mean_score": _mean(scores),
        "score_range": (max(scores) - min(scores)) if len(scores) > 1 else None,
        "score_std": statistics.pstdev(scores) if len(scores) > 1 else None,
        "per_field_mean": per_field, "flip_rate_by_field": flips,
        "max_flip_rate": max((v for v in flips.values() if v is not None), default=None),
        "unsupported": sum(r["unsupported"] for r in flat), "extracted_fields": extracted,
        "parse_errors": sum(r["parse_errors"] for r in flat), "retries": sum(r.get("retries", 0) for r in flat),
        "mean_in_tokens": _mean([r["in"] for r in flat]), "mean_out_tokens": _mean([r["out"] for r in flat]),
        "mean_reasoning_tokens": _mean([r["reasoning"] for r in flat]),
        "cost_per_call": _mean([r["cost"] for r in flat]), "total_cost": sum(r["cost"] for r in flat),
        "mean_latency_ms": _mean([r["ms"] for r in flat]),
    }


def cache_key(version: str, model: str, effort: Optional[str], rep: int, deal_id: str) -> str:
    return f"{version}|{model}|{effort or 'default'}|{rep}|{deal_id}"


def run_spec(settings: Settings, paths: Paths, deal_ids: list, *, model: str, effort: Optional[str],
             template: Optional[str], repeats: int, cache_path: Path, tag: str, workers: int = 6,
             client=None) -> list:
    """Run `repeats` full passes over `deal_ids`. Resumable: records already in the cache (same prompt
    version, model, effort, repeat, deal) are reused. Returns a list of per-repeat lists of records."""
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    version = prompt_version(template)
    cache: dict = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    jobs = [(rep, d) for rep in range(repeats) for d in deal_ids]

    def work(job):
        rep, d = job
        key = cache_key(version, model, effort, rep, d)
        if key in cache and "error" not in cache[key]:
            return key, rescore_proposals(paths, cache[key])
        return key, run_instance(settings, conn, paths, d, model, effort, f"{tag}-{rep}", template, client)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, rec in pool.map(work, jobs):
            cache[key] = rec
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return [[cache[cache_key(version, model, effort, rep, d)] for d in deal_ids] for rep in range(repeats)]


def instance_outcomes(runs: list) -> list:
    """Per run: {(deal, field): correct}, for flip counting."""
    return [{(r["deal_id"], f): r["fields"][f]["correct"] for r in run if "error" not in r for f in FIELDS}
            for run in runs]


def instance_credits(runs: list) -> dict:
    """{(deal, field): [credit per run]}, for paired comparison."""
    out: dict = {}
    for run in runs:
        for r in run:
            if "error" in r:
                continue
            for f in FIELDS:
                out.setdefault((r["deal_id"], f), []).append(r["fields"][f]["score"])
    return out


def run_report(label: str, runs: list, *, model: str, effort: Optional[str], prompt: str, split: str) -> dict:
    """Aggregate plus the noise numbers a paired test needs: instances whose outcome flips between runs."""
    agg = aggregate(label, runs)
    outcomes = instance_outcomes(runs)
    n_instances = len(set.intersection(*[set(o) for o in outcomes])) if outcomes else 0
    flipped = stats.flip_instances(outcomes)
    agg.update({"split": split, "model": model, "effort": effort, "prompt_version": prompt,
                "instances_per_run": n_instances, "flipped_instances": flipped,
                "flipped_instance_rate": flipped / n_instances if n_instances else None})
    return agg


def estimate_cost(settings: Settings, model: str, avg_in_tokens: float, n_calls: int,
                  assumed_out_tokens: int = 700) -> float:
    p_in, p_out = settings.price(model)
    return n_calls * (avg_in_tokens * p_in + assumed_out_tokens * p_out) / 1_000_000
