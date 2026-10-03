"""Extractor bake-off: V0 on a fixed set of validation transcripts across model/effort settings.

Reads answer keys in code only. The main session reads results/bakeoff.json (aggregates), never the
per-instance file results/bakeoff_runs.json.
"""

from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import db
from .config import Settings
from .deals import DealSet
from .evalrun import _mean, aggregate, run_instance  # noqa: F401
from .extractor import build_prompt, extract, load_template, prompt_version
from .ingest import parse_transcript
from .paths import Paths
from .schema import CASE_IDS, FIELDS

N_DEALS = 15
SEED = 20261001
LUNA_EFFORTS = ("none", "low", "medium")
FLIP_LIMIT = 0.10  # max per-field flip rate between repeats for a setting to count as stable
CEILING = 0.95  # mean field score at or above this leaves no headroom for the learning loop
EST_OUT_TOKENS = 900  # dry-run guess only


def select_deals(dealset: DealSet, n: int = N_DEALS, seed: int = SEED) -> list:
    """Stratified pick from validation using only case labels: cover every case id, include clean calls."""
    pool = sorted(d["deal_id"] for d in dealset.deals.values() if d["split"] == "validation")
    rng = random.Random(seed)
    rng.shuffle(pool)
    cases = {i: {c["id"] for c in dealset.deals[i]["cases"]} for i in pool}
    chosen: list = []
    uncovered = set(CASE_IDS)
    while uncovered and len(chosen) < n:
        best = max((i for i in pool if i not in chosen), key=lambda i: len(cases[i] & uncovered), default=None)
        if best is None or not cases[best] & uncovered:
            break
        chosen.append(best)
        uncovered -= cases[best]
    for i in pool:
        if len(chosen) >= n:
            break
        if i not in chosen and not cases[i] and sum(not cases[c] for c in chosen) < 2:
            chosen.append(i)
    for i in pool:
        if len(chosen) >= n:
            break
        if i not in chosen:
            chosen.append(i)
    return sorted(chosen)


def plan(settings: Settings, repeats: int) -> list:
    """[(label, model, effort, repeat_index)]: Luna at each effort x repeats, then Sol once."""
    out = [(f"luna-{e}", settings.extractor_model, e, r) for e in LUNA_EFFORTS for r in range(repeats)]
    out.append(("sol", settings.learner_model, None, 0))
    return out


def choose(aggs: list) -> dict:
    """Pick the cheapest Luna setting that is stable, not at the ceiling, and within noise of the best Luna.

    "Within noise" means its mean is no further below the best Luna mean than the larger of the two settings'
    run-to-run score ranges. "Stable" means every field's flip rate between repeats is at most FLIP_LIMIT.
    """
    luna = [a for a in aggs if a["setting"].startswith("luna") and a["repeats"] > 1 and a["calls"]]
    if not luna:
        return {"pick": None, "reason": "no Luna setting with repeats"}
    best = max(luna, key=lambda a: a["mean_score"])
    rows = []
    for a in luna:
        noise = max(a["score_range"] or 0.0, best["score_range"] or 0.0)
        rows.append({"setting": a["setting"], "stable": (a["max_flip_rate"] or 0) <= FLIP_LIMIT,
                     "headroom": a["mean_score"] < CEILING,
                     "within_noise_of_best": best["mean_score"] - a["mean_score"] <= noise,
                     "cost_per_call": a["cost_per_call"], "noise_used": noise})
    ok = [r for r in rows if r["stable"] and r["headroom"] and r["within_noise_of_best"]]
    pick = min(ok, key=lambda r: r["cost_per_call"])["setting"] if ok else None
    return {"pick": pick, "best_luna": best["setting"], "flip_limit": FLIP_LIMIT, "ceiling": CEILING,
            "candidates": rows,
            "reason": "cheapest candidate that is stable, below the ceiling, and within noise of the best Luna"
                      if pick else "no setting met all three conditions; decide manually"}


def estimate(settings: Settings, paths: Paths, deal_ids: list, repeats: int) -> dict:
    calls = plan(settings, repeats)
    toks = []
    for d in deal_ids:
        p = paths.transcripts / f"{d}.md"
        if p.exists():
            toks.append(len(build_prompt(parse_transcript(p.read_text(encoding="utf-8")))) / 4)
    avg_in = _mean(toks) if toks else 0.0
    cost = 0.0
    for _, model, _, _ in calls:
        p_in, p_out = settings.price(model)
        cost += len(deal_ids) * (avg_in * p_in + EST_OUT_TOKENS * p_out) / 1_000_000
    return {"calls": len(calls) * len(deal_ids), "avg_input_tokens": avg_in, "assumed_output_tokens": EST_OUT_TOKENS,
            "estimated_cost_usd": cost, "prices_set": all(settings.price(m) != (0.0, 0.0) for _, m, _, _ in calls)}


def smoke(settings: Settings, conn, paths: Paths, fixture: Path, client=None) -> list:
    """One call per setting on a fixture transcript (no key): confirms model ids, effort values, usage fields."""
    text = fixture.read_text(encoding="utf-8")
    body = text[text.index("Call type"):text.index("--- EVIDENCE")]
    block = parse_transcript("Date: 2026-09-18\n" + body)
    out = []
    for label, model, effort, _ in plan(settings, 1):
        try:
            ex = extract(settings, conn, block, model=model, effort=effort, run_id="smoke", client=client)
            out.append({"setting": label, "ok": not ex.errors, "parse_errors": ex.errors[:3],
                        "in": ex.llm.input_tokens, "out": ex.llm.output_tokens, "reasoning": ex.llm.reasoning_tokens,
                        "cost": ex.llm.cost_usd, "ms": ex.llm.latency_ms})
        except Exception as e:
            out.append({"setting": label, "ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"})
    return out


def run_bakeoff(settings: Settings, paths: Paths, deal_ids: list, repeats: int, workers: int = 6) -> dict:
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    runs_path = paths.root / "results" / "bakeoff_runs.json"
    cache: dict = json.loads(runs_path.read_text()) if runs_path.exists() else {}
    template = load_template()
    version = prompt_version(template)
    jobs = [(label, model, effort, rep, d) for label, model, effort, rep in plan(settings, repeats) for d in deal_ids]

    def key_of(label, rep, d):
        return f"{version}|{label}|{rep}|{d}"

    def work(job):
        label, model, effort, rep, d = job
        key = key_of(label, rep, d)
        if key in cache and "error" not in cache[key]:
            return key, cache[key]
        return key, run_instance(settings, conn, paths, d, model, effort, f"bakeoff-{label}-{rep}", template)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for key, rec in ex.map(work, jobs):
            cache[key] = rec
    runs_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")

    aggs = []
    for label in [f"luna-{e}" for e in LUNA_EFFORTS] + ["sol"]:
        reps = sorted({int(k.split("|")[2]) for k in cache if k.startswith(f"{version}|{label}|")})
        runs = [[cache[key_of(label, r, d)] for d in deal_ids if key_of(label, r, d) in cache] for r in reps]
        aggs.append(aggregate(label, runs))
    result = {"prompt_version": version, "deals": deal_ids, "n_deals": len(deal_ids), "repeats": repeats,
              "models": {"luna": settings.extractor_model, "sol": settings.learner_model},
              "settings": aggs, "selection": choose(aggs),
              "caveat": "15 transcripts x 3 repeats is small; chosen on the validation set (selection, not teaching)."}
    (paths.root / "results" / "bakeoff.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
