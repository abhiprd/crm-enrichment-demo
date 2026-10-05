"""Small stdlib statistics for the eval harness: flip rates, exact McNemar, paired bootstrap."""

from __future__ import annotations

import math
import random
from typing import Optional


def flip_instances(outcomes_by_run: list) -> int:
    """outcomes_by_run: one {instance: correct(bool)} dict per run. Count instances whose outcome is not
    the same in every run (an instance missing from a run is ignored)."""
    if not outcomes_by_run:
        return 0
    common = set.intersection(*[set(o) for o in outcomes_by_run])
    return sum(len({o[k] for o in outcomes_by_run}) > 1 for k in common)


def mcnemar_exact(improved: int, worsened: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts (instances that changed)."""
    n = improved + worsened
    if n == 0:
        return 1.0
    k = min(improved, worsened)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_bootstrap(diffs: list, n_boot: int = 10000, seed: int = 0, alpha: float = 0.05) -> tuple:
    """(mean, lo, hi): percentile CI of the mean per-instance difference (candidate minus baseline)."""
    if not diffs:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo, hi = means[int(alpha / 2 * n_boot)], means[int((1 - alpha / 2) * n_boot) - 1]
    return sum(diffs) / n, lo, hi


def compare(base: dict, cand: dict, noise_flips: int, seed: int = 0) -> dict:
    """Paired comparison on shared instances. `base` and `cand` map instance -> list of per-run credits.

    An instance counts as correct when its mean credit over runs is at least 0.5 (majority-correct); it
    changed when that outcome differs. A change clears the noise floor only if the McNemar p-value is
    below 0.05 AND the changed instances outnumber the instances that flip between the base arm's own runs (V0's for V0 pairings).
    """
    keys = sorted(set(base) & set(cand))
    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    improved = sum(mean(base[k]) < 0.5 <= mean(cand[k]) for k in keys)
    worsened = sum(mean(cand[k]) < 0.5 <= mean(base[k]) for k in keys)
    diffs = [mean(cand[k]) - mean(base[k]) for k in keys]
    d, lo, hi = paired_bootstrap(diffs, seed=seed)
    p = mcnemar_exact(improved, worsened)
    changed = improved + worsened
    return {"instances": len(keys), "improved": improved, "worsened": worsened, "changed": changed,
            "mcnemar_p": p, "mean_diff": d, "diff_ci95": [lo, hi], "noise_flipped_instances": noise_flips,
            "clears_noise_floor": bool(p < 0.05 and changed > noise_flips)}
