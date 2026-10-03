# Status

Update at the end of every session: what changed, what's next, and any number with its source file.

| # | Milestone | State | Evidence |
| --- | --- | --- | --- |
| M0 | Setup and vertical slice | done | Live run 2026-10-02: Approve in Slack changed HubSpot `amount` on d007 from 130000 to 60000; `seed_hubspot.py --live --reset` restored 130000. Reject path also exercised (HubSpot unchanged). Log: `results/crm.sqlite` (git-ignored) |
| M1 | Deals, 100 transcripts, rendering audit | done | `python3 -m crm status`: all 100 ingested and verified (learn 40, validation 40, test 20). Hand-check: 0 of 15 sheets had errors, per `results/handcheck/result.json` (reviewer-reported; 95% upper bound on the error rate about 0.20 at n=15, so the verifier pass on all 100 is the stronger evidence) |
| M2 | Extractor, validator, dry-run eval, noise floor, manual baseline | started: V0 extractor, validator, scorer, bake-off | `results/bakeoff.json`; `results/v0_noise.json` and `results/manual_baseline.json` do not exist yet |
| M3 | Slack review loop and HubSpot writeback | not started | |
| M4 | Learning loop | not started | |
| M5 | Batch experiment and test run | not started | |
| M6 | Charts and unit economics | not started | |
| M7 | Write-up and demo | not started | |

## Decisions pending

D1 to D5 in `PLAN.md`.

D6: capturing stakeholders who need convincing (skeptics, blockers) is out of scope for v1. Decided 2026-10-02: ignore; no schema change. Mention as a limitation in the write-up.

## Log

- 2026-10-01: Scaffold created. `crm ingest`, `crm request`, `crm status` with 20 passing tests. Three example deals in `data/deals.json`.
- 2026-10-01 (M0): pulled `scripts/make_deals.py` into M0 and generated the 100-deal set (seed 1337, 40/40/20, 20 clean, 15 marked for HubSpot seeding). Replaced the three example deals; they live on as `tests/fixtures/deals.json` for the ingest tests. Added `crm/config.py`, `db.py` (SQLite schema), `llm.py` (pinned model, spend logged), `hubspot.py`, `review.py`, `slack_app.py`, `slice.py`, `scripts/seed_hubspot.py`. Dry-run slice passes: approve writes once, a second click is a no-op.

## Extractor bake-off (2026-10-02, audited by `eval-auditor`)

Source: `results/bakeoff.json` (`python3 -m crm eval bakeoff --run`, prompt `v0-71968bd8`). 150 extraction calls on 15 validation transcripts (Luna none/low/medium x 3 repeats = 135, Sol x 1 run = 15), 0/150 API errors, total spend $0.353.

Metric: mean per-field credit over 135 instances per run (15 transcripts x 9 fields). Not F1 or accuracy.

| Setting | Run scores | Mean | Cost per call |
| --- | --- | --- | --- |
| Luna `none` | 0.751, 0.773, 0.768 | 0.764 | $0.0005 |
| Luna `low` | 0.723, 0.701, 0.731 | 0.719 | $0.0007 |
| Luna `medium` | 0.731, 0.738, 0.738 | 0.736 | $0.0011 |
| Sol (1 run) | 0.844 | 0.844 | $0.0167 |

Cost per call is at the configured prices ($0.10/$0.50 per 1M tokens for Luna, $2/$10 for Sol); Sol is 33x Luna `none` per call.

- On these 15 transcripts, higher effort did not improve Luna's mean field score (low is 0.045 and medium 0.028 below `none`). With 3 repeats and no paired test, this is an observation, not a result that clears the noise floor.
- Mean per-field flip rate across repeats (flip = correct/incorrect differs; 135 instances): `none` 12/135 (0.089), `low` 16/135 (0.119), `medium` 16/135 (0.119). Within noise. `low` logged 10 parse/field-validity errors across 45 calls (0 for the others).
- Sol's single run is 0.081 above Luna `none`. Directional only: no repeat spread, no paired test. Sol is the rule learner, so this is a ceiling reference.
- The pre-set rule (max per-field flip <= 0.10, mean < 0.95, within noise of best) returned no pick: 0 of 3 settings stable. Choosing `OPENAI_EXTRACTOR_EFFORT=none` is a manual judgment: cheapest and highest-scoring Luna setting here.
- Pain points and use case score low for every setting (Sol 6/15 and 7/15 transcripts' credit; Luna `none` 12/45 and 17/45). These need exact taxonomy-set matches. Worth checking the scoring and keys before M2 builds on them.
- Caveats: deals were chosen by case-stratified selection from validation, and the setting was chosen on validation. `crm/scoring.py` requires exact timeline matches (stricter than SPEC section 6) and gives 1/3 credit per next-step part.

## Open items

- Bake-off cache in `results/bakeoff_runs.json` is not keyed by prompt version. Key it before rerunning after any prompt change.
- Decide: document or loosen timeline scoring against SPEC section 6; redefine the stability rule (a 10% per-field flip limit is unreachable at n=15).
- Full 5-run noise floor on the full validation set (`/eval-run noise`) is still to do. Nothing may be called beyond the noise floor until it exists.
- Rendering audit: the first 15 had 4 failures (a not-mentioned field hinted at); the generator prompt gained rule 7, the 4 were regenerated, and all 15 now pass verification. Hand-check about 15 transcripts for the real rendering error rate.
- Install Python 3.11 (only 3.9 here; `pyproject.toml` requires 3.11+). Code runs on 3.9.
- Bot token lacks `channels:read`, so channel membership can't be verified; posting works.
- D1 to D5 were built to the PLAN.md recommendations but not explicitly confirmed.
- 2026-10-02 (M0 closed): added `crm preflight` (HubSpot read/write scope probes that create nothing, Slack scopes and Socket Mode token, OpenAI models); the live seed and live slice refuse to run unless it passes. Review card now shows Account Name (id), Deal Name (id) linked to the HubSpot record, and the quote with its speaker. Seeded 15 fictional deals into HubSpot.
- 2026-10-02 (M1): generated and verified all 100 transcripts. Verifier first pass failed some calls (a not-mentioned field hinted in the dialogue was the common cause; d084 took three attempts, d044 three), and ingest rejected two files for missing evidence; all were regenerated and now pass. These first-pass counts are not yet computed from a results file, so no rendering error rate is claimed. Added generator rule 7 and a house-rule clarification to the verifier. `scripts/handcheck_sheet.py` builds 15 stratified sheets in `results/handcheck/`; they contain answer keys, so the main Claude session does not open them.
- Known data issue: d036 combines a `committed_champion` case with a champion-owned next step (intro to buyer); the transcript satisfies both only by having the rep assign the step. Left as is (truth is fixed); list it as ambiguous in the write-up.
- 2026-10-02 (M1 closed): hand-check of 15 sheets found no rendering issues (`results/handcheck/result.json`). Schema gap noted by the reviewer: a stakeholder who needs convincing is not captured by the nine fields. See Decisions pending.
