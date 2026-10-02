---
description: Run an eval experiment (noise | baseline | curve | ablate | test)
argument-hint: noise|baseline|curve|ablate|test
---
Run experiment `$ARGUMENTS` per SPEC Section 6. All runs are dry-run against answer keys, via code in this repo, with the OpenAI model pinned in config and spend logged by `crm/llm.py`.

- `noise`: V0 on the validation set 5 times; write `results/v0_noise.json` with per-field flip rates.
- `baseline`: manual-baseline prompt (learn set only for tuning); write `results/manual_baseline.json`.
- `curve`: oracle reviewer through the learn set in batches of 5, scoring validation after each version; write `results/curve.json`.
- `ablate`: rules only, few-shot only, both, on validation; write `results/ablation.json`.
- `test`: STOP first. If `results/test_run.json` exists, do not run; ask me. Otherwise confirm with me that the final version is frozen, then run V0, manual baseline, and final once and write `results/test_run.json`.

Never run anything on the test split except `test`. Never open `data/keys/`, `data/audit/`, or validation/test `fields` in the main session; only code does. Report results as counts with denominators, and call a change "within noise" unless it clears the noise floor. Results go to README/STATUS only after the `eval-auditor` agent checks them.
