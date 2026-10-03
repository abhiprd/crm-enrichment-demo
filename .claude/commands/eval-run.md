---
description: Run an eval experiment (noise | baseline | curve | ablate | test)
argument-hint: noise|baseline|curve|ablate|test
---
Run experiment `$ARGUMENTS` per SPEC Section 6. All runs are dry-run against answer keys, via code in this repo, with the OpenAI model pinned in `.env` and spend logged by `crm/llm.py`. Every command prints a call plan and cost estimate unless `--run` is given.

- `noise`: `python3 -m crm eval noise [--run]`. V0 on the full validation split, 5 runs; writes `results/v0_noise.json` (run scores, flip rates, flipped instances = the bar for paired tests) and the per-instance `results/v0_noise_runs.json`.
- `baseline`: iterate the prompt on the LEARN split only with `python3 -m crm eval learn --prompt prompts/extractor_baseline.md --round N --run` and `eval learn-errors --prompt ...` (learn errors only, at most 5 rounds, logged in `results/baseline_iterations.json`). Freeze the prompt, show the project owner the diff, and only after approval run `python3 -m crm eval baseline --run --approved`. Writes `results/manual_baseline.json` with the paired comparison against V0.
- `curve`: oracle reviewer through the learn set in batches of 5, scoring validation after each version; write `results/curve.json` (M5).
- `ablate`: rules only, few-shot only, both, on validation; write `results/ablation.json` (M5).
- `test`: STOP first. If `results/test_run.json` exists, do not run; ask me. Otherwise confirm with me that the final version is frozen, then run V0, manual baseline, and final once and write `results/test_run.json`.

Never run anything on the test split except `test`. Never open `data/keys/`, `data/audit/`, `results/*_runs.json`, or validation/test `fields` in the main session; only code does (learn-split keys may be read while tuning prompts). Report results as counts with denominators. A change is a result only if it clears the pre-set noise criterion (paired test p < 0.05 and more changed instances than V0's flipped instances); otherwise call it "within noise". Results go to README/STATUS only after the `eval-auditor` agent checks them.
