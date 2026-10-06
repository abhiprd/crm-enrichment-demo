# Interaction-to-CRM Updates

Turn a sales call transcript into proposed HubSpot deal updates, have a human approve them in Slack, and measure how well the extraction holds up.

```
call transcript  ->  extractor (LLM)  ->  quote check  ->  proposals  ->  Slack review card  ->  HubSpot
                                                                |
                                                  approve / edit / reject
```

Everything runs in dry-run by default. Posting to Slack or writing to HubSpot needs an explicit `--live` flag.

## Findings in brief

- **The mechanism works end to end.** Calls become Slack cards, reviewer decisions are logged, and a reject can become a versioned, gated, revertible rule that changes the next extraction (live demo and paired replay on two fictional calls, no statistics, `results/m4_demo.json`). The rules from that demo did not clear the validation gate.
- **Run-to-run noise is large enough to matter.** Identical V0 runs flip 37 of 360 (transcript, field) outcomes at truth level (`results/v0_noise.json`) and 25 of 360 at proposal level (`results/ablation.json`, `noise_flipped_instances`). A change counts only if it also changes more instances than that and passes the paired test.
- **A scored gain exists only on one metric.** With an oracle reviewer, the proposal-level score rises on validation (tuned-on) and on the single test run, but the truth-level score on the test run is lower than the hand-tuned baseline (0.850 vs 0.926, `results/test_run.json`). The largest single contribution is reviewer-edit examples fixing the decision-timeline quarter format (about 13 of the roughly 27 net instance gain in the final state; `results/ablation.json`, `results/proposal_scores.json`); the rest is spread over economic buyer, champion and competitors, where the three gated rules act. That is a per-field decomposition on tuned-on validation, not an isolated ablation. Rules alone changed 21 of 360 instances (19 improved, 2 worsened) against a bar of 25: within noise.
- **Some conventions were not learned.** The learner declined to write a rule for all 6 budget and 2 next-step rejects it examined (all reason `not_crm_worthy`), so the ballpark-budget convention was never picked up (0 of 7 instances at every point, `results/curve.json`).
- **The oracle is not a human.** One reviewer matched the oracle's decision kind on 45 of 58 proposals on 10 validation calls (not held out; `results/handreview.json`).

The details, denominators and caveats are under Results and in [STATUS.md](STATUS.md).

## What it can do

- **Extract nine CRM fields from a call**: budget, decision timeline, competitors (with stance), economic buyer, champion, pain points, use case, next steps, and a stage signal. Each value carries a status (stated, hedged, negated, superseded, or not mentioned) and the quote it came from.
- **Drop values whose quote is not in the call**: a value is only proposed if its quote appears in the cited utterance (4 of 1,532 extracted fields failed in the V0 noise runs, `results/v0_noise.json`). This checks the quote, not whether the value is right.
- **Propose, never overwrite**: extractions are diffed against the deal's current HubSpot values. Hedged values are marked tentative, negated competitors become `ruled_out`, lists are merged without duplicates, and the stage signal becomes a deal note, never a stage change.
- **Review in Slack**: one card per call with every proposal, the quotes behind it (speaker and timestamp), a link to the deal, and Approve / Edit / Reject on each field. Only the record owner or a manager can act. There is no approve-all button.
- **Write back safely**: the CRM value is re-read before each write; if it changed since the proposal, the card refreshes instead of writing. Double clicks are ignored.
- **Watch a folder**: drop a demo transcript in `inbox/` and `ingest --watch --run --live` posts the card.
- **Seed a sandbox**: an idempotent script creates fictional companies and deals in HubSpot (with a reset), and `crm preflight` checks keys, scopes, models, and Slack tokens before any live step.
- **Measure extraction quality**: 100 synthetic transcripts with answer keys, split 40 learn / 40 validation / 20 test. A run-to-run noise floor, a manual prompt baseline and paired statistics label each change as clearing or within noise on validation (which is tuned-on); the 20-transcript test split was run once.
- **Learn from reviewers**: rejects become rules and edits become worked examples for the extractor; each rule is replayed on the validation split by a gate (in demo mode a rule acts immediately, flagged unvalidated) and versioned, with revert.
- **Log everything** to SQLite (interactions, extractions, proposals, review decisions) and track model spend per run.

The answer keys and eval truth are not published in this repository (`data/keys/`, `data/audit/`, `data/deals.json`, and per-instance run files are git-ignored); `scripts/make_deals.py` regenerates a deal set. All companies and people are fictional (`.example` domains). Competitor names are real vendors, as reps actually say them.

## Quick start

Python 3.9+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env        # add your keys; never commit .env
python3 -m pytest -q
python3 -m crm status       # dataset progress by split
python3 -m crm preflight    # verify keys, scopes, and models before live steps
```

Live demo (needs HubSpot, Slack, and OpenAI keys in `.env`; run it with the virtual environment active). Comments are kept out of the commands so they paste into zsh. The full on-camera script is in [docs/DEMO.md](docs/DEMO.md).

```bash
source .venv/bin/activate
python3 scripts/seed_hubspot.py --demo --live
python3 -m crm ingest --watch --run --live --force
cp prompts/demo/northwind.md inbox/demo-northwind.md
python3 scripts/seed_hubspot.py --demo --live --reset
```

In order: create the demo deal; watch `inbox/` and post cards to Slack (leave this running in its own terminal tab); drop a call so a card appears; restore the demo deal afterwards.

Evaluation (dry-run plan first; add `--run` to call the model):

```bash
python3 -m crm eval noise [--run]       # V0 noise floor on the validation split
python3 -m crm eval baseline [--run --approved]
```

## Results

Scores come in two kinds. **Truth level** asks whether the extraction matches what was said on the call. **Proposal level** asks whether it leads to the proposal that should land in the CRM after company house rules (for example, timelines are recorded as quarters). House rules are never shown to the extractor; the loop has to learn them from reviewer feedback. All numbers below come from files in `results/` and are generated into the charts by `python3 -m crm charts`. The detail, denominators, and caveats are in [STATUS.md](STATUS.md).

![Validation score by learning batch](docs/charts/curve.svg)

An oracle reviewer (a script that plays the reviewer from the answer key) went through the 40 learn transcripts in 8 batches; after each batch the extractor was scored on the 40 validation transcripts. At proposal level (3 runs per point), the 33 house-rule instances per run rose from 0.313 to 0.616 and the all-field score from 0.815 to 0.890 (`results/curve.json`). At truth level the same runs went from 0.865 to 0.851. Validation was also used to gate rules, so this line is tuned-on, not held out.

![Ablation](docs/charts/ablation.svg)

On validation (5 runs per arm, 360 instances), V0 scored 0.807, rules only 0.848, examples only 0.843 and both 0.891 (`results/ablation.json`). Against V0's 25 flipped instances, examples only changed 30 (22 improved, 8 worsened) and both changed 40 (37 improved, 3 worsened), so both clear the pre-set noise rule. Rules only changed 21 (19 improved, 2 worsened), which is within noise. The ablation tests the final state, not individual rules, and these are tuned-on validation numbers.

![Test split](docs/charts/test.svg)

On the single test run (20 transcripts x 9 fields = 180 instances, 5 runs per arm, `results/test_run.json`), proposal-level scores were V0 0.818, manual baseline 0.852 and final 0.912. Final vs baseline changed 18 of 180 instances (14 improved, 4 worsened; p = 0.031 against a bar of 8 flips), which clears the pre-set rule. **At truth level the order reverses**: V0 0.879, baseline 0.926, final 0.850 (no paired test stored, so "lower", not "significantly lower"). The final version was tuned toward the proposal metric, so its gain is not better extraction against the labels. The test split has been used once and must not be run again.

The oracle's edit-or-reject share by batch (with Wilson intervals) and its reject reasons are below. The bands overlap, and the extractor changed between batches, so no trend is tested.

![Error rate](docs/charts/review_error.svg)

![Reject reasons](docs/charts/reject_reasons.svg)

![Rule inventory](docs/charts/rules.svg)

Of 11 candidate rules drafted from rejects, 3 passed the validation gate. The learner declined to write a rule for 9 rejects, including every budget and next-step reject it examined, so the ballpark-budget house rule stayed at 0 of 7 instances at every point (`results/curve.json`). The oracle gave only a reason code, no note; whether the cause is that, the learner, or an unlearnable rule was not isolated.

A single reviewer (the project owner) also reviewed 58 proposals on 10 validation calls. Their decision kind matched the oracle's on 45 of 58 (`results/handreview.json`); these are validation calls (not held out) and the reviewer knew the learn-split work, so it is a small, single-reviewer sample and the oracle is not a proven stand-in for a human.

### Unit economics

From `results/unit_economics.json`, built from local spend logs (git-ignored, so it regenerates only locally). These are per model request, not per transcript or deal.

| Item | Value |
| --- | --- |
| Extractor requests at the selected setting (`gpt-6-luna`, effort `none`) | 5,691 logged, pooled across every validation, test, noise-floor, baseline, gate, ablation and demo run at that setting, including archived first-pass runs; excludes retries and the effort-low and effort-medium bake-off calls |
| Mean cost per extractor request | $0.000525 |
| Latency per extractor request | 4.8 s mean, 4.3 s median, 8.0 s at the 95th percentile |
| Tokens per extractor request (means; prompts vary by run) | about 2,484 in, 553 out |
| Rule learner (`gpt-6.1-sol`, effort low) | 22 requests, $0.0015 each, 3.3 s mean |
| Learning-curve run | $2.04 (`results/curve.json`) |
| Ablation | $0.33 (`results/ablation.json`) |
| Test run | $0.16 (`results/test_run.json`) |
| CRM field slots held a value, hand-reviewed calls | 36 of 80 before the call; 62 of 80 after the reviewer's approved or edited proposals |

The curve, ablation and test costs are subsets of the logged requests, not additions to them, and none of this is the project's total spend. The fill-rate row counts filled slots (10 validation calls x 8 fields), not correct values; it comes from one reviewer's decisions, whose clicks went to a stub and wrote nothing to HubSpot, so it is a demonstration, not a rate to expect elsewhere.

## Limitations

- **Synthetic data.** 100 fictional calls written by one model family, with traps and house rules placed by a generator. A hand-check of 15 stratified calls (reviewer-reported) found 0 of 15 with rendering errors (Wilson 95% upper bound about 0.20, assuming the sample is representative). All 100 passed the Claude verifier after regeneration; first-pass failures were not counted, so no verifier error rate is claimed. Real calls are messier, and the house rules here are far denser than in a real CRM.
- **One extractor model, pinned.** Results are for one small model at one effort setting, on 40 validation and 20 test transcripts. No claim is made that they generalise.
- **Validation is tuned-on.** It informed the prompt redesign, the effort choice, rule gating, the curve and the ablation. The test split is the only held-out check, and it has been used once.
- **The headline metric was chosen after seeing earlier results.** Proposal-level scoring is in the plan (D1), but the baseline-versus-V0 comparison was pre-set at truth level, where it was within noise; re-scored at proposal level it clears the bar by one instance. Treat that as borderline.
- **The oracle is idealised.** It never errs and, when it edits, supplies the answer key's convention, so the curve is an upper bound on what a perfect reviewer's feedback yields. The hand review is one reviewer and 58 proposals, with no per-field significance.
- **Rules are global and few.** v1 has one reviewer, so a rule applies to everyone; in a real organisation one rep's rejection may be personal preference and rules should be per-rep until validated. Per-reviewer weighting is a design note in the spec, not built.
- **The gate is strict and was left unchanged.** Its "other fields may lose at most 2" limit is smaller than the measured run-to-run flip allowance on those fields, so it may retire rules that are harmless. Its outcome metric changed to proposal level in M5; its thresholds did not.
- **Scoring choices.** Pain points and use cases are scored by exact set match; next steps by the best match among the extracted steps, while the proposal builder uses only the first. Competitor names use an alias map. These were fixed after some results were seen and apply to every arm.
- **Not captured.** Stakeholders who need convincing (skeptics, blockers) are outside the nine fields (decision D6). One deal (d036) combines a committed champion with a champion-owned next step and is ambiguous.
- **Pipeline gaps.** The pipeline can let a junk stance value through when the extractor lists a vendor without a valid stance. A rule's text is generated from the reviewer's note and the call; a name typed in a note is not checked against the rule. Timers, escalation and the weekly digest were cut. `--reset` restores deal properties but does not delete the stage-signal notes created during review.
- **Ops.** Socket Mode needs the laptop running, so the demo has a recorded fallback. The Slack bot token lacks `channels:read`, so channel membership is not verified. Spend numbers cover completed requests only, and the unit-economics figures pool every run at the selected setting.
- **Nothing here writes without a human click.** Auto-write to the CRM is a path-to-production note, not a feature; the dry-run error rates are the case for the human gate.

## Demo

A step-by-step 2-minute runbook is in [docs/DEMO.md](docs/DEMO.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `crm/` | The package: pipeline, extractor, validator, proposal builder, review logic, Slack and HubSpot clients, eval harness, CLI |
| `data/` | Deals and answer keys, taxonomy and label definitions, transcripts, audit verdicts |
| `prompts/` | Extractor prompts, transcript generator prompt, the demo transcript |
| `scripts/` | Deal generator, HubSpot seeding, hand-check worksheets |
| `results/` | Eval outputs (noise floor, baseline, bake-off, curve, ablation, test run, hand review, unit economics) |
| `docs/charts/` | The static SVG charts used above, regenerated by `python3 -m crm charts` |
| `tests/` | Unit and end-to-end dry-run tests |
| `docs/SPEC.md`, `PLAN.md`, `STATUS.md` | Specification, build plan, and current progress with measured results |

## Stack

Python, SQLite, Slack Bolt (Socket Mode), the HubSpot REST API (service key), and OpenAI models for extraction and rule learning. Synthetic transcripts are written by Claude; the extractor is a different model family, so it never grades text written in its own style.

## Milestones

**Completed**

- **M0, Setup and vertical slice**: model interface with spend logging, HubSpot client and idempotent seeding with reset, Slack approval that lands in HubSpot, preflight checks.
- **M1, Dataset**: 100 fictional deals with traps and house rules, transcripts generated and independently verified, hand-checked sample.
- **M2, Extraction and measurement**: extractor, quote validator, scorer, noise floor, manual baseline, paired statistics, model and effort bake-off.
- **M3, Slack review loop and writeback**: batched review cards with edit and reject modals, authorization, stale-value handling, deal notes, folder watcher, live demo.
- **M4, Learning loop**: a reject becomes a plain-English rule written by a second model, put in force immediately (demo mode) or after validation, replayed on the validation split by a gate, stored in append-only versions that can be reverted. Demonstrated live on a paired call (no validated gain).

- **M5, Experiments**: oracle reviewer, learning curve, ablation, the single test-set run, and a human-versus-oracle reviewer comparison.
- **M6, Charts and unit economics**: static charts (below) and a cost and latency table, generated from `results/` by `python3 -m crm charts`.

**To do**

- **M7, Write-up and demo**: the write-up and limitations are above; the recording is still to be made from the runbook.

Measured results and per-milestone evidence are tracked in [STATUS.md](STATUS.md).
