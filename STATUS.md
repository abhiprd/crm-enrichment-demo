# Status

Update at the end of every session: what changed, what's next, and any number with its source file.

| # | Milestone | State | Evidence |
| --- | --- | --- | --- |
| M0 | Setup and vertical slice | done | Live run 2026-10-02: Approve in Slack changed HubSpot `amount` on d007 from 130000 to 60000; `seed_hubspot.py --live --reset` restored 130000. Reject path also exercised (HubSpot unchanged). Log: `results/crm.sqlite` (git-ignored) |
| M1 | Deals, 100 transcripts, rendering audit | done | `python3 -m crm status`: all 100 ingested and verified (learn 40, validation 40, test 20). Hand-check: 0 of 15 sheets had errors, per `results/handcheck/result.json` (reviewer-reported; 95% upper bound on the error rate about 0.20 at n=15, so the verifier pass on all 100 is the stronger evidence) |
| M2 | Extractor, validator, dry-run eval, noise floor, manual baseline | done | `results/v0_noise.json`, `results/manual_baseline.json`, `results/baseline_iterations.json`, `results/bakeoff.json`; auditor-checked wording below |
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

## Extractor bake-off (rerun 2026-10-03 under the final timeline metric, audited by `eval-auditor`)

Source: `results/bakeoff.json`. On 15 validation transcripts x 3 repeats, mean scores were luna-none 0.775, luna-low 0.751, luna-medium 0.728 (run ranges 0.000, 0.030, 0.059). Sol, 1 run (15 calls), scored 0.793. Higher effort did not improve Luna here, and none was cheapest at $0.000485 per call. The automatic selector chose no setting (pick null) and `none` was selected manually (`OPENAI_EXTRACTOR_EFFORT=none`). Small sample; chosen on validation (selection, not teaching).

Metric: mean per-field credit over transcripts x 9 fields; not F1 or accuracy. Timeline scoring follows SPEC section 6: a predicted date inside a quarter truth counts; a date truth needs that exact day.

## V0 noise floor (`results/v0_noise.json`, audited)

V0 (pv-71968bd8, gpt-6-luna, effort none), 5 runs x 40 validation transcripts (200 calls, 0 API errors): run scores 0.8296/0.8194/0.8407/0.8370/0.8296, mean 0.8313, range 0.0213, population std 0.0073. 41 of 360 (transcript, field) instances flipped correct/incorrect between runs (0.114). 5 of 1565 extracted fields failed the quote check. 10 parse/field-validity errors and 3 retries (one retry is made when a reply is unparseable).

## Manual baseline (`results/manual_baseline.json`, audited)

Prompt `prompts/extractor_baseline.md`, tuned on the learn split only, frozen after round 2, 5 runs x 40 validation transcripts (200 calls): run scores 0.9028/0.9250/0.9167/0.9222/0.9139, mean 0.9161, range 0.0222; 29 of 360 instances flip. Per-field means: budget 0.975, timeline 0.990, competitors 1.000, economic_buyer 0.980, champion 0.875, pain_points 0.780, use_case 0.760, next_step 0.945, stage_signal 0.940 (V0: pain_points and use_case 0.520 each).

**Verdict under the pre-set criterion (McNemar p < 0.05 AND changed instances > V0's flipped instances): the baseline's gain over V0 is within noise.** 35 changed instances (32 improved, 3 worsened) against a bar of 41 flipped; the criterion was not met. Exact McNemar p = 4.2e-7 (32 vs 3 discordant).

Post hoc, not the pre-set rule: the mean score difference is 0.0848 (0.9161 vs 0.8313), bootstrap 95% CI [0.0563, 0.1143]. V0's run-to-run range is 0.0213 and the baseline's 0.0222; the two sets of run scores do not overlap (V0 max 0.8407, baseline min 0.9028). This is descriptive, not a claim of clearing the floor.

Limits of the criterion (disclosed, rule not changed): "changed" counts instances whose 5-run mean crosses 0.5 while "flipped" counts instances that differ in any of 5 runs, so the two are not like for like and the bar is biased against passing. McNemar and the bootstrap treat 360 instances as independent although they cluster by 40 transcripts, so p and the CI are optimistic. A matched noise bar (majority-outcome changes between splits of V0's own runs) would be a post-hoc amendment and is not applied.

Disclosure for the write-up: the manual baseline prompt includes generic competitor-stance guidance that partially overlaps the `competitor_threshold` house rule, so the loop's headroom on that rule is reduced (competitors: V0 0.917 to baseline 1.000, `results/` files above).

Learn-split single-run scores while tuning (`results/baseline_iterations.json`; 40 transcripts, 1 run each): round 0 (V0) 0.837, round 1 0.907, round 2 0.938. Round 0 ran before the unparseable-reply retry was added. These are tuning scores on the data the prompt was tuned against, not generalisation evidence.

Process note: the project owner approved the frozen baseline prompt in chat on 2026-10-03 before it was scored (the `--approved` flag gates the run); no results file records this.

Spend logged in `llm_calls` for these experiments: $1.07 (1027 calls), of which about $0.45 was discarded reruns (an earlier noise batch without retry, and the bake-off before the timeline-metric change). The reported results account for $0.62: noise $0.097, baseline $0.104, learn rounds $0.059, bake-off $0.358.

Validation was used for the effort choice and for the baseline-vs-V0 comparison, so any later learning-loop claim must be confirmed on the single test run. Prompt tuning used the learn split only.

## Open items

- Decide whether to add a labelled post-hoc matched noise criterion for paired comparisons (see Manual baseline). The pre-set criterion stands until then.
- Rendering audit: the first 15 had 4 failures (a not-mentioned field hinted at); the generator prompt gained rule 7, the 4 were regenerated, and all 15 now pass verification. Hand-check about 15 transcripts for the real rendering error rate.
- Install Python 3.11 (only 3.9 here; `pyproject.toml` requires 3.11+). Code runs on 3.9.
- Bot token lacks `channels:read`, so channel membership can't be verified; posting works.
- D1 to D5 were built to the PLAN.md recommendations but not explicitly confirmed.
- 2026-10-02 (M0 closed): added `crm preflight` (HubSpot read/write scope probes that create nothing, Slack scopes and Socket Mode token, OpenAI models); the live seed and live slice refuse to run unless it passes. Review card now shows Account Name (id), Deal Name (id) linked to the HubSpot record, and the quote with its speaker. Seeded 15 fictional deals into HubSpot.
- 2026-10-02 (M1): generated and verified all 100 transcripts. Verifier first pass failed some calls (a not-mentioned field hinted in the dialogue was the common cause; d084 took three attempts, d044 three), and ingest rejected two files for missing evidence; all were regenerated and now pass. These first-pass counts are not yet computed from a results file, so no rendering error rate is claimed. Added generator rule 7 and a house-rule clarification to the verifier. `scripts/handcheck_sheet.py` builds 15 stratified sheets in `results/handcheck/`; they contain answer keys, so the main Claude session does not open them.
- Known data issue: d036 combines a `committed_champion` case with a champion-owned next step (intro to buyer); the transcript satisfies both only by having the rep assign the step. Left as is (truth is fixed); list it as ambiguous in the write-up.
- 2026-10-02 (M1 closed): hand-check of 15 sheets found no rendering issues (`results/handcheck/result.json`). Schema gap noted by the reviewer: a stakeholder who needs convincing is not captured by the nine fields. See Decisions pending.
- 2026-10-03 (M2 closed): timeline scoring now follows SPEC; added `crm/stats.py` (flip rate, exact McNemar, paired bootstrap), `crm/evalrun.py`, `crm/evalcmds.py` (`eval noise|learn|learn-errors|baseline`), cache keyed by prompt version with raw extractions, one retry on unparseable replies, and routed the preflight model check through `crm/llm.py` (rule 7). Ran the 5-run noise floor and the manual baseline; auditor-checked wording above. Open: 59 tests pass; test split untouched.
