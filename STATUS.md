# Status

Update at the end of every session: what changed, what's next, and any number with its source file.

| # | Milestone | State | Evidence |
| --- | --- | --- | --- |
| M0 | Setup and vertical slice | done | Live run 2026-10-02: Approve in Slack changed HubSpot `amount` on d007 from 130000 to 60000; `seed_hubspot.py --live --reset` restored 130000. Reject path also exercised (HubSpot unchanged). Log: `results/crm.sqlite` (git-ignored) |
| M1 | Deals, 100 transcripts, rendering audit | done | `python3 -m crm status`: all 100 ingested and verified (learn 40, validation 40, test 20). Hand-check: 0 of 15 sheets had errors, per `results/handcheck/result.json` (reviewer-reported; 95% upper bound on the error rate about 0.20 at n=15, so the verifier pass on all 100 is the stronger evidence) |
| M2 | Extractor, validator, dry-run eval, noise floor, manual baseline | done (second pass) | `results/v0_noise.json`, `results/manual_baseline.json`, `results/baseline_iterations.json`, `results/bakeoff.json`; auditor-checked wording below; first pass archived |
| M3 | Slack review loop and HubSpot writeback | done | Live run 2026-10-03: a demo transcript dropped in `inbox/` produced one card; 9 proposals logged in `results/crm.sqlite` (git-ignored): 8 approved, 1 rejected (with reason and note); HubSpot values and the stage-signal note matched the approvals; `seed_hubspot.py --demo --live --reset` restored the demo deal |
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

## Extractor bake-off (`results/bakeoff.json`, audited; run with the first-pass prompt pv-71968bd8)

On 15 validation transcripts x 3 repeats under the final timeline metric, mean scores were luna-none 0.775, luna-low 0.751, luna-medium 0.728 (run ranges 0.000, 0.030, 0.059). Sol, 1 run (15 calls), scored 0.793. Higher effort did not improve Luna here, and none was cheapest at $0.000485 per call. The automatic selector chose no setting (pick null) and `none` was selected manually (`OPENAI_EXTRACTOR_EFFORT=none`). Small sample; chosen on validation.

Metric (all sections): mean per-field credit over transcripts x 9 fields; not F1 or accuracy. Timeline follows SPEC section 6 (a predicted date inside a quarter truth counts; a date truth needs that exact day). Pain points and use cases are scored by exact set match. next_step is a list scored as best match against the single keyed step; extra steps are ignored.

## M2 results (second pass, `results/v0_noise.json` and `results/manual_baseline.json`, audited)

"V0" here is the updated starting prompt (pv-ca8faa57): V0 plus per-label definitions for pain points and use cases (`data/taxonomy_definitions.json`), a champion definition, a best-fit-label instruction, and next_step as a list. It is not the first-pass V0 (pv-71968bd8). The first pass is archived in `results/archive/m2_first_pass/` and superseded.

**V0 noise floor.** 5 runs x 40 validation transcripts = 200 calls, 0 API errors: run scores 0.8648/0.8583/0.8741/0.8704/0.8611, mean 0.8657, range 0.0157, population std 0.0058. 37 of 360 (transcript, field) instances flipped correct/incorrect between runs (0.103). 4 of 1532 extracted fields failed the quote check. 5 unparseable-reply retries. Per-field means: budget 0.965, timeline 0.990, competitors 0.878, economic_buyer 0.850, champion 0.870, pain_points 0.760, use_case 0.600, next_step 0.928, stage_signal 0.950.

**Manual baseline** (the frozen baseline prompt, pv-6bc5055e, tuned on the learn split over two rounds in the first pass and re-run under the new schema). 5 runs x 40 validation = 200 calls, 0 recorded API errors (an earlier attempt hit rate limits and those calls were re-run; this is not recorded in `results/`): run scores 0.9056/0.9111/0.9111/0.9194/0.9083, mean 0.9111, range 0.0139. 25 of 360 instances flip. 5 of 1478 extracted fields failed the quote check. 6 unparseable-reply retries. Per-field means: budget 0.965, timeline 1.000, competitors 1.000, economic_buyer 0.975, champion 0.825, pain_points 0.830, use_case 0.720, next_step 0.910, stage_signal 0.975.

**Verdict under the pre-set criterion (McNemar p < 0.05 AND changed instances > V0's flipped instances): within noise.** Baseline beat V0 on 20 of 360 validation instances and lost on 6 (26 changed; exact McNemar p = 0.0094; mean difference +0.0454, bootstrap 95% CI [0.0252, 0.0674]). It does not clear the rule because 26 < 37, the number that flipped between V0's own runs. This is not called significant. Changed-instance and flipped-instance counts are not like for like, and instances are clustered by transcript (40), so the p-value is optimistic.

The baseline is worse than V0 on champion (0.825 vs 0.870) and on next_step (0.910 vs 0.928).

**Why the gap narrowed.** The gap fell from 0.0848 (first pass: 0.9161 vs 0.8313) to 0.0454. V0 rose by 0.0344, mostly in pain_points (0.520 to 0.760) and use_case (0.520 to 0.600), alongside the added label definitions; the baseline moved by -0.0050. This is consistent with the definitions giving V0 part of what the baseline's guidance supplied. It was not isolated by an ablation, and V0 also changed in champion, next_step and output format.

Learn-split tuning scores (`results/baseline_iterations.json`, single runs, 40 learn transcripts): updated V0 0.879, frozen baseline 0.937. These are tuning scores, not generalisation evidence. No tuning rounds were run in the second pass; the baseline guidance is unchanged from the first pass except the next_step wording. Validation was not used for tuning.

Spend counts completed responses only (rate-limited calls produced no usage record). Second-pass cycle: noise $0.101, baseline $0.111, learn rounds $0.041; 492 calls, $0.253 in `llm_calls` since the archive step. The first-pass cycle is in the archive.

**Process and leakage disclosures.**
- The decisions to add label definitions and make next_step a list were made after seeing first-pass validation results (V0 pain_points and use_case 0.520), so validation informed the redesign. Validation is no longer a clean held-out set: it was used for the effort choice, the first-pass comparison and this comparison. The test split stays the single confirmation.
- The next_step scoring rule (best match over a list, extras ignored) was changed after those results. It is lenient toward list-producing prompts and is applied to both arms.
- The label definitions were drafted from the label names and the learn-split error analysis; no validation or test transcript was read. The project owner approved the definitions, the champion definition and the baseline prompt in chat.
- The baseline prompt includes generic competitor-stance guidance that partially overlaps the `competitor_threshold` house rule, so the loop's headroom on that rule is reduced.
- Exact set match for pain points and use cases was kept by the project owner's choice; per-label partial credit is not applied. It can be added later from the saved raw extractions.

## Open items

- Decide whether to add a labelled post-hoc matched noise criterion for paired comparisons (see Manual baseline). The pre-set criterion stands until then.
- Rendering audit: the first 15 had 4 failures (a not-mentioned field hinted at); the generator prompt gained rule 7, the 4 were regenerated, and all 15 now pass verification. Hand-check about 15 transcripts for the real rendering error rate.
- Bot token lacks `channels:read`, so channel membership can't be verified; posting works.
- D1 to D5 were built to the PLAN.md recommendations but not explicitly confirmed.
- 2026-10-02 (M0 closed): added `crm preflight` (HubSpot read/write scope probes that create nothing, Slack scopes and Socket Mode token, OpenAI models); the live seed and live slice refuse to run unless it passes. Review card now shows Account Name (id), Deal Name (id) linked to the HubSpot record, and the quote with its speaker. Seeded 15 fictional deals into HubSpot.
- 2026-10-02 (M1): generated and verified all 100 transcripts. Verifier first pass failed some calls (a not-mentioned field hinted in the dialogue was the common cause; d084 took three attempts, d044 three), and ingest rejected two files for missing evidence; all were regenerated and now pass. These first-pass counts are not yet computed from a results file, so no rendering error rate is claimed. Added generator rule 7 and a house-rule clarification to the verifier. `scripts/handcheck_sheet.py` builds 15 stratified sheets in `results/handcheck/`; they contain answer keys, so the main Claude session does not open them.
- Known data issue: d036 combines a `committed_champion` case with a champion-owned next step (intro to buyer); the transcript satisfies both only by having the rep assign the step. Left as is (truth is fixed); list it as ambiguous in the write-up.
- 2026-10-02 (M1 closed): hand-check of 15 sheets found no rendering issues (`results/handcheck/result.json`). Schema gap noted by the reviewer: a stakeholder who needs convincing is not captured by the nine fields. See Decisions pending.
- 2026-10-03 (M2 closed): timeline scoring now follows SPEC; added `crm/stats.py` (flip rate, exact McNemar, paired bootstrap), `crm/evalrun.py`, `crm/evalcmds.py` (`eval noise|learn|learn-errors|baseline`), cache keyed by prompt version with raw extractions, one retry on unparseable replies, and routed the preflight model check through `crm/llm.py` (rule 7). Ran the 5-run noise floor and the manual baseline; auditor-checked wording above. Open: 59 tests pass; test split untouched.
- 2026-10-03 (M2 second pass): added label definitions and a champion definition (`data/taxonomy_definitions.json`, prompts), next_step as a scored list, and rate-limit/transient-error backoff in `crm/llm.py`. Re-ran the noise floor and manual baseline; auditor-checked wording above. 61 tests pass; test split untouched.
- 2026-10-03 (M3): added `crm/proposals.py` (extraction to proposals per SPEC status table), `crm/pipeline.py` (parse, match deal by external company name, read current CRM values, extract, quote-check, log, propose, post), per-field Approve/Edit/Reject with modals, owner-or-manager authorization, stale re-diff, set/append/clear/note writeback, `ingest --watch --run --live` (demo calls only; eval transcripts never reach the pipeline), a demo deal (`data/demo_deals.json`, `scripts/seed_hubspot.py --demo`), notes and owner probes in `crm preflight`. Bug found in the live run and fixed: the card showed only the first cited quote; it now shows every cited quote with speaker and time (up to four). Skipped by decision: timers, escalation channel, weekly digest, and any approve-all control. Optional evidence note on approve not built.
- M3 notes: the extractor inferred a `forecasting` use case the demo call never stated, and the reviewer approved the use-case row anyway; this is the kind of reviewer signal M4 learns from, not a bug. `--reset` restores properties but does not delete the stage-signal notes created during review.
