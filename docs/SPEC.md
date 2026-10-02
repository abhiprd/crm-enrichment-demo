# GTME Project 1: Interaction-to-CRM Updates (Spec)

Sep 30, 2026, revised Oct 1 · Abhi

Canonical copy: https://claude.ai/code/artifact/972bb544-bae2-4ae4-8802-6e5e58919413 (this file is a Markdown export for Claude Code; PLAN.md wins where they differ).

## 1. Scope and success criteria

The system turns a call transcript into proposed HubSpot deal-field updates, routes each through Slack for the account manager to approve, edit, or reject, then uses those decisions to improve itself and measure the improvement. The headline result is the learning curve on house rules: company conventions the extractor is never told, learned from reviewer feedback alone. Accuracy metrics are the measuring stick it rises against, and a time-boxed manual prompt is the bar it must clear.

**In scope (v1)**

- 100 synthetic transcripts with ground-truth labels: about 80% carry 2–3 trap or house-rule cases, about 20% are clean
- 9 deal fields, one LLM extractor, HubSpot free CRM via API
- A folder watcher as the ingest trigger, standing in for a call-recorder webhook
- Slack review in channels, with owner-only click authorization
- Versioned rule learning with a validation gate
- Eval harness, static results charts, and unit economics (cost and latency per call)

**Out of scope (v1)**

- Audio and speech-to-text, real customer data, real companies, outbound email generation
- Multi-call deal merging (later project) and contact-role association
- Auto-write (process note only, see Section 6) and writing stage changes to HubSpot
- Reviewer weighting (design note only, see Section 4)

**Success metrics**

| Metric | Definition | Why it matters |
| --- | --- | --- |
| Field-level precision, recall, F1 | A field is correct only if value and status both match ground truth. Reported overall, by trap type, and by house rule. | Real accuracy, and the baseline the learning curve rises from |
| Unsupported-value rate | Share of extracted values whose cited quote does not match the cited utterance after normalizing whitespace, quote marks, and punctuation | Deterministic hallucination check, no LLM judge |
| Evidence accuracy | Cited utterance overlaps the ground-truth supporting utterances | Trust in the evidence the reviewer sees |
| Dry-run error rate | Share of proposals that disagree with ground truth, by field | What an ungated system would have written wrong. The case for the human gate. |
| Edit rate and reject rate | (edits + rejects) / reviewed, per field, tracked separately | Shows which fields are closest to needing less review |
| Learning curve | Validation F1 by ruleset version, for traps and house rules separately, against V0 and the manual-prompt baseline | Headline: proof the loop works |
| Unit economics | Cost and latency per call, and CRM field fill rate before vs. after | The business case a GTM leader reads first |

**Decisions**

1. Every write is human-gated in v1. Auto-write is a process note (Section 6), not a v1 build.
2. About 80% of transcripts carry 2–3 cases: traps (negations, hedged timelines, superseded values, speaker-attribution traps, buried next steps) and house rules. About 20% are clean.
3. The learning curve counts only if it matches or beats a time-boxed manual prompt.
4. No numeric targets until the baseline run exists.
5. No result is reported until the noise floor is measured (Section 6).

## 2. Data and ground truth

The eval set is 100 synthetic transcripts, each with a ground-truth answer key, split so that rules are learned, gated, and finally tested on different calls. Truth comes first: each deal in `deals.json` defines its true field values, and Claude renders them into a transcript on the subscription (the transcript-writer agent in Claude Code, or a claude.ai chat). OpenAI extracts from it, so the extractor never grades text written in its own style.

**Split**

| Set | Transcripts | Used for |
| --- | --- | --- |
| Learn | 40 | Feeds the oracle reviewer. Rejects and edits here produce candidate rules and few-shot examples. |
| Validation | 40 | Gates every candidate rule and draws the learning curve. Never used for learning. |
| Test | 20 | Touched once, at the end, to compare V0, the manual-prompt baseline, and the final version. |

Each set is stratified by case type (trap or house rule) and by CRM relationship, so every set holds the same mix of clean and adversarial calls and of matching, stale, and empty CRM fields.

**Deals and truth.** `deals.json` defines 100 fictional deals, one per transcript, and is the single source of truth. For each field, a deal sets the true value and status, plus a CRM relationship: matches the call, stale (the call contradicts it), or empty (the call fills it). It also lists the traps and house rules the call carries. Without CRM relationships, the generator echoes seeded values and the eval is mostly no-change cases. Each call is scored against a frozen snapshot, so approving one proposal never changes the next transcript's starting state.

**Fictional companies.** Every deal is an invented company on a `.example` domain with invented people. The reason is publishing, not contamination: a public repo should not hold fabricated call quotes attributed to real firms. Contamination is low risk because most fields are call-local facts (budget, people, dates) that no outside knowledge supplies, and the evidence check (Section 3) blocks any value without a supporting quote. Competitor names are real vendors, as reps actually say them. Real companies belong to the signal-trigger project.

**Case mix.** About 80% of transcripts carry 2–3 cases, drawn from the traps and house rules below. About 20% are clean, to measure false proposals. Traps are applied subtly so no speaker points them out:

- **negation:** "we're not looking at Competitor X"
- **hedged_timeline:** "we'd probably land this by end of year"
- **superseded_value:** a number or date changes mid-call, and the final value wins
- **speaker_attribution:** the champion and a skeptical stakeholder say different things
- **buried_next_step:** a commitment appears late and in passing

**House rules.** Company conventions the extractor prompt is never told. The oracle reviewer enforces them, so V0 gets them wrong by design and the loop has real room to improve. This mirrors production, where rep feedback mostly encodes company conventions rather than reading comprehension. v1 uses 4–5 (draft list):

- **competitor_threshold:** only competitors under formal evaluation count; a passing mention is not CRM-worthy
- **dated_next_step:** a next step without a date is not CRM-worthy
- **ballpark_budget:** a figure called a rough ballpark is not a budget
- **quarter_timeline:** decision timelines are recorded at quarter granularity
- **committed_champion:** a champion must have committed to an internal action, not just shown enthusiasm

**Answer key.** For each of the 9 fields, the key holds the true value, a status (stated, hedged, negated, superseded, not_mentioned), and the supporting utterance idx, plus the field's CRM value before the call (crm_before). Fields not mentioned are marked not_mentioned, never omitted, so recall on absent fields can be scored. Values and statuses are copied from deals.json; the generator supplies only the supporting idx.

**Rendering audit.** Keys are correct by construction, so the risk moves to rendering: a transcript that fails to express a truth, expresses it too plainly, or cites the wrong utterance. A separate verifier agent checks each truth against its cited utterance, and failures are regenerated. Hand-check about 15 transcripts and report the rendering error rate. If it is above about 5%, fix the generator prompt before building further.

**Transcript format.** One line per turn, `[idx] [mm:ss] Speaker (Role): text`, which maps directly to the utterances table in Section 3.

**File layout.** `transcripts/<deal_id>.md` holds only what real call metadata would: participants, date, call type, then the utterances. `keys/<deal_id>.json` holds the case labels (traps and house rules), split, CRM relationship, `crm_before`, and the answer key. Nothing about the cases or the key appears in the transcript file.

**Live-demo prompt.** Live demos use this chat template, where Claude picks the facts and reveals the key on request. The bulk generator does not use it: it renders truths from deals.json. Run extraction in a separate session so the generator's context cannot leak into it.

```text
Generate a synthetic B2B sales call transcript to test a CRM-extraction system.

Parameters (I'll fill in, or choose for me):
- Vendor: [fictional company + what it sells]
- Prospect: [fictional company, size, industry]
- Call type: [discovery | demo | negotiation]
- Trap: [none | negation | hedged_timeline | superseded_value | speaker_attribution | buried_next_step]
- Length: ~[N] turns

Rules:
1. Output the transcript only, one line per turn: [idx] [mm:ss] Speaker (Role): text
   Make it natural: filler, tangents, interruptions, small talk. No tidy recap lines
   unless a real person would say them.
2. Work in 4-6 of these facts, stated indirectly and scattered, as real people do:
   budget, decision timeline, competitors (and stance), economic buyer, champion,
   pain points, use case, next step (owner + date). Leave at least two unmentioned.
3. Apply the trap subtly. No speaker should point it out.
4. Do NOT output an answer key. Stop after the transcript. When I type REVEAL, output
   JSON: for each of the 9 fields, the ground-truth value, status
   (stated|hedged|negated|superseded|not_mentioned), and supporting utterance idx.
```

## 3. Extraction schema

The schema separates what was said (extractions) from what to write (proposals), and every extracted claim cites the smallest possible evidence: an utterance id and an exact quote. The reviewer never sees extractions, only proposals: a CRM field diff with the supporting quote. The rules and ruleset_versions tables are in Section 4.

```text
interactions: id, source_type (call|email|note|slack), occurred_at,
              participants[{name, org, role, is_internal}], deal_ref, source_meta (json)
utterances:   id, interaction_id, idx, speaker, is_internal, start_ts, text

extractions   (CRM-agnostic: what was said; one row per field per interaction)
  field, value (typed), status: stated | hedged | negated | superseded | not_mentioned,
  confidence, evidence[{utterance_id, quote}], prompt_version, model, ruleset_version_id

proposals     (CRM mapping layer: what to write; one row per proposed write)
  extraction_id, hubspot_object, property, current_value, proposed_value,
  action: set|append|clear, tentative (bool),
  review_status: pending|approved|edited|rejected|no_response|bulk_approved,
  final_value, reject_reason, reviewed_by, reviewed_at, slack_ts, ruleset_version_id
```

**v1 fields**

| Field | Value type |
| --- | --- |
| Budget | Amount (normalized number), or a range with both bounds |
| Decision timeline | Date or period, at the granularity stated |
| Competitors | List of (name, stance: evaluating, ruled_out, incumbent) |
| Economic buyer | Person |
| Champion | Person |
| Pain points | Taxonomy enum (about 8 categories), plus a free-text summary for display |
| Use case | Taxonomy enum (about 8 categories), plus a free-text summary for display |
| Next step | Action (enum), owner, date |
| Stage signal | Enum: advance, hold, regress. Logged as a deal note, never written to deal stage. |

**Status values**

| Status | Meaning | Proposal behavior |
| --- | --- | --- |
| stated | Said plainly | Proposed |
| hedged | Said but not committed ("maybe Q2") | Proposed with a tentative tag |
| negated | Explicitly ruled out | Proposed only if it changes what the CRM says: a competitor becomes ruled_out (added as ruled_out if absent), and a value the CRM still holds is cleared. Otherwise no proposal. |
| superseded | Changed mid-call. The final value wins and the earlier one stays in the evidence. | Final value proposed |
| not_mentioned | Never discussed | No proposal. Kept as a row so recall on absent fields can be scored. |

**Design rules**

1. **Source normalization.** A flat common core plus a `source_meta` JSON blob for source-specific fields.
2. **Extraction and CRM mapping stay separate,** so the same extraction can feed outbound or cross-call mining later without another LLM call.
3. **Deterministic hallucination check.** Every quote must be an exact substring of its cited utterance after normalizing whitespace, quote marks, and punctuation, so formatting noise does not count as hallucination. A failure is logged as unsupported and never reaches Slack.
4. **Hedged values are proposed, not suppressed.** Rejections of hedged values are exactly the learning signal the loop needs.

**Extractor isolation.** The extractor never sees CRM values, so extractions stay CRM-agnostic and cannot anchor on seeded data. Only the proposal builder diffs against HubSpot. In dry-run it reads `crm_before` from the answer key instead.

## 4. Pipeline architecture and rule learning

The pipeline is one linear path from transcript to HubSpot write, with a feedback edge from reviewer decisions back into the extractor's rules. The same path runs in dry-run mode, with no Slack or HubSpot, for batch evaluation.

```text
Parse -> Extractor (LLM + active rules + few-shot) -> Validator (quote must match)
  -> fails: Unsupported (logged, never proposed)
  -> pass:  Proposal builder (diff vs current HubSpot value) -> Slack review
            -> on click: Action handler (stale check, write, log)
            -> Rule learner (rejects: new rules; edits: examples)
            -> Validation gate (replay on 40 validation calls)
            -> pass: Ruleset version (append-only, revertible) -> next run of the Extractor
```

A reject becomes a candidate rule. It reaches the extractor as a new ruleset version only if it passes the validation gate, and a rule that fails the gate stays a candidate.

**Ingest.** A folder watcher picks up each new transcript file and starts the pipeline, standing in for a call-recorder webhook. Swapping in a real webhook changes only the entry point.

**Stack.** Python, SQLite, Pydantic, slack-bolt in Socket Mode, HubSpot REST through a service key (HubSpot stopped new private-app creation: at once for accounts created on or after Sept 28, 2026, and for all accounts on Oct 26, 2026), and APScheduler for timers. OpenAI runs the extractor and the rule learner, with the model pinned and kept behind one interface. Claude generates the transcripts on the subscription, not the API.

**Rule learning**

- A rule is written by an LLM from a rejection: the reject reason, the quote, and the extraction. It is scoped to one field, with a cap of about 5 active rules per field.
- An edit (proposed value to final value) becomes a few-shot example. The 3 most recent per field are injected.
- **Candidate, validate, activate.** A candidate rule is replayed on the 40 validation transcripts. It activates only if the target field gains more correct instances than the noise floor allows, by a paired test on the instances that changed, and no other field loses more than it allows.
- **Demo mode.** In live demos a rule activates immediately, flagged unvalidated, and validates asynchronously.
- **Scope.** v1 has one reviewer, so rules are global. In a real org one rep's rejection may be personal preference, so rules should be per-rep until validated. This is a stated limitation, not a hidden one.

**Versioning and traceability**

History is append-only. Nothing is mutated, and a revert is a new version that copies an earlier version's active rule set. Every extraction and proposal carries its `ruleset_version_id`, so any past output can be reproduced and attributed.

```text
rules:            rule_id, field, rule_text, rationale, source_proposal_ids[],
                  created_by, created_at, status: candidate|active|retired|reverted
ruleset_versions: version_id, parent_version_id, active_rule_ids[], prompt_template_hash,
                  change_type: add|edit|retire|revert, changed_rule_id, reason, author,
                  validation_score_before, validation_score_after, created_at
```

The rationale is generated from the triggering rejects (reason, quote, failure type, expected effect) and can be edited with a human note.

**Reviewer weighting (design note, not v1)**

The goal is to stop a bad actor from degrading accuracy by weighting each reviewer by whether their feedback actually helped.

1. **Credit assignment.** Each rule stores `source_proposal_ids`. When a rule is validated or reverted, its validation delta is credited or debited to the reviewers behind those proposals.
2. **Ground-truth proxy.** Production has no oracle, so use a random audit sample (a trusted reviewer re-checks about 5% of approvals and rejects), cross-reviewer agreement on the same pattern, and later CRM corrections.
3. **Weight.** Beta-posterior shrinkage, so a new reviewer starts neutral and moves slowly. Use the lower confidence bound, not the mean, and add time decay.
4. **What the weight controls.** The summed weight of supporting reviewers must clear a threshold to promote a candidate rule. It also ranks few-shot examples. Low-weight feedback is quarantined to that reviewer's own scope, never deleted.

Caveats: a rule that lowers global accuracy may reflect a legitimate preference, so scope rules instead of dinging taste as error. The validation gate and versioned revert are the real defense. Weights risk entrenching the majority view, so keep the audit sample independent of them.

## 5. HubSpot writeback and Slack review

The account manager reviews every proposed deal update in Slack, and an approved or edited value is written to HubSpot only if the CRM value has not changed since the proposal was made.

**HubSpot**

- **Deals only.** Use native properties for amount and next step, and custom properties for the rest (decision timeline, competitors, champion, economic buyer, pain points, use case), grouped as "AI-extracted." Decision timeline is not mapped to close date, since a buyer's decision date is not the close date. Stage signal goes into a deal note, never deal stage. Verify internal property names and free-tier custom-property limits in M0.
- **Contacts.** Champion and economic buyer are plain text on the deal in v1. Contact-role association is v2.
- **Seed data.** About 15 deals from deals.json, half with prefilled or stale values so the diffs look real. A seed script creates them (see Seeding below).
- **Multi-value fields** append and dedupe.
- **Write safety.** Re-read the current value before the PATCH and refresh the card if it changed. The handler acts only while `review_status` is pending, which covers double-clicks and Slack retries.
- **Optional.** On approve, attach a deal note with the quote, timestamp, and approver, so the evidence lives in the CRM.

**Seeding.** `deals.json` is the source of truth, and `seed_hubspot.py` is idempotent. It creates the "AI-extracted" property group and custom properties if missing, batch-creates companies and then deals, associates each deal to its company, assigns the reviewer as owner, and writes the returned HubSpot IDs back into `deals.json` so later runs update instead of duplicating. `--reset` PATCHes every seeded deal back to its `crm_before` values, so each demo or review run starts clean.

Only about 15 deals are seeded because batch evals are dry-run and read `crm_before` from the answer keys. The other 85 exist as frozen snapshots. Service key scopes: deals and companies read and write, deal schemas read and write (for the custom properties), and owners read. If a second HubSpot owner is unavailable, the manager is a second Slack user mapped to the same owner. Verify the endpoints, scopes, and free-tier record caps in M0.

**Slack channels**

| Channel | Used for |
| --- | --- |
| interaction-to-crm-updates-review | Review cards: approve, edit, reject |
| interaction-to-crm-updates-manager-escalation | Proposals left unreviewed past the escalation timer |
| interaction-to-crm-updates-weekly-digest | Weekly aggregates only |

The destination is a config value, because `chat.postMessage` takes a channel ID or a user ID. Moving the review flow to real DMs later is a one-line change. The escalation and digest channels are needed only if the stretch items below are built.

**Review card**

1. One card per interaction with all fields batched, and the record owner @mentioned so the notification reaches the right person. HubSpot owner email maps to the Slack user.
2. Each field shows current value to proposed value, the quote, speaker, and timestamp, with Approve, Edit, and Reject buttons.
3. Edit opens a modal prefilled with the proposed value. Reject opens a modal with a reason dropdown and an optional note.
4. The card updates in place after each action: Approved, Edited with the new value, or Rejected with the reason.
5. **Click authorization.** The handler checks that the clicker is the mapped record owner or the manager. Anyone else gets an ephemeral "only the owner can review this."
6. **No Approve-all button.** Rubber-stamping corrupts the feedback signal. If added for convenience, it logs as `bulk_approved` and is excluded from acceptance metrics.
7. **Reject reasons:** wrong_value, wrong_speaker, stale_or_superseded, hedged_not_committed, not_crm_worthy, evidence_doesnt_support.

```text
Northwind call, 2:30pm (34 min): 3 proposed updates   @owner

Decision timeline   Oct 15 -> 2026-Q4  (tentative)
> "we'd probably land this by end of year"  Priya, 21:14
[Approve] [Edit] [Reject]

Competitor     none -> Gong (evaluating)
> "we're comparing you against Gong right now"  Priya, 12:02
[Approve] [Edit] [Reject]

Next step      (empty) -> Send security questionnaire, owner: you, by Oct 7
> "can you get us the SOC 2 stuff by next Friday"  Dan, 30:40
[Approve] [Edit] [Reject]
```

**Timers** (stretch goal, cut by default; config, compressed to minutes for live demos)

- 4 hours: reminder in the card's thread.
- 24 hours: post to the escalation channel with the manager @mentioned.
- 48 hours: expire as `no_response`. Silence is never counted as a rejection, because that would poison the learning loop.

**Weekly digest.** Stretch goal, cut by default. Aggregates only: counts by field and outcome, top reject reasons, rules added, acceptance trend. No deal values or quotes, since the channel is broader than the owner. A `--now` flag triggers it on demand for demos.

**Slack setup.** Invite the bot to all three channels. Scopes are `chat:write`, `users:read`, and `users:read.email`, plus an app-level token for Socket Mode. Verify current Slack free-plan limits before building.

## 6. Eval harness

Every result comes from the same pipeline run in dry-run mode against the answer keys, scored per field type, and no change counts as a result unless it exceeds the measured run-to-run noise. Metric definitions are in Section 1.

**Scoring by field type.** A field is correct only if value and status both match ground truth.

| Field | Value match |
| --- | --- |
| Budget | Normalized amount (`$50k` equals `50000`). A range matches only if both bounds match. |
| Decision timeline | Extracted date falls inside the truth period, at the granularity stated ("end of Q4" does not equal "Dec 31"). |
| Competitors | Set of (name, stance) pairs, scored P/R/F1 per pair, with an alias map for name normalization. |
| Economic buyer, champion | Normalized person match. Catches speaker-attribution traps. |
| Pain points, use case | Taxonomy enum, exact match. The free-text summary is for display only. |
| Next step | Action (enum), owner, and date scored separately. |
| Stage signal | Enum, exact. |

Pain points and use case use a taxonomy so scoring is deterministic. Free text would need an LLM judge, which adds a second noisy model to the eval.

**Noise floor.** LLM runs are nondeterministic even at temperature 0. Run V0 on the validation set 5 times and record how often each field instance flips between runs. A change counts as a result only if a paired test (McNemar or bootstrap) on the instances that changed clears that flip rate. Anything else stays out of the write-up.

**Learning-curve experiment**

1. Score V0 on the validation set 5 times. This is the starting grade before any teaching, and it measures the noise floor.
2. Build the manual baseline: V0 plus one hour of hand prompt-engineering, using the learn set only. This is the bar the loop must clear.
3. Feed the learn set (40) through the oracle reviewer in batches of 5. After each batch, snapshot the ruleset version and score it on the validation set, for traps and house rules separately. That line is the learning curve. Validation calls are never used for teaching, or the system would just memorize them.
4. Freeze the final version and run the test set (20) once: V0, the manual baseline, and the final version. Running once means no tweaking until it looks good.
5. Ablate on validation: rules only, few-shot examples only, both. This shows which kind of feedback actually helped.
6. Optional contamination check: rerun 15 validation transcripts with real company names swapped in and diff the extractions. This measures contamination instead of assuming it.

**Oracle reviewer.** A script plays the reviewer using the answer key and calls the same Slack handler code path programmatically.

- Proposal matches truth: approve.
- Right field, wrong value: edit, supplying the true value.
- Truth is not_mentioned or negated but a value was proposed: reject, with a reason code derived from the transcript's trap type.
- A house rule excludes the value: reject as not_crm_worthy. A house rule changes its format: edit to the conforming value.

It is an idealized reviewer: it never rubber-stamps a wrong value, never rejects a correct one, and always gives an accurate reason. Its learning curve is an upper bound. So about 10 transcripts are hand-reviewed in Slack, and the write-up reports the gap between human and oracle outcomes.

**Results charts.** Static charts generated from the SQLite log and embedded in the README, so a viewer needs nothing running. Trends are plotted by ruleset version, not calendar date, because transcript difficulty varies run to run and a date axis mixes input mix with real improvement.

| Chart | Shows |
| --- | --- |
| Validation score by version | Field F1 and unsupported rate on the fixed validation set, for traps and house rules separately, with V0 and the manual baseline as reference lines and rule add, retire, and revert events marked. This line proves the loop. |
| Error rate by field by version | (edits + rejects) / reviewed, with Wilson intervals. Small n gives wide bands, and they are shown. |
| Reject-reason mix | Should shift away from the failure types a rule addressed |
| Rule inventory | Active, candidate, retired, reverted counts, and validated vs. unvalidated |
| Unit economics | Cost and latency per call, and CRM field fill rate before vs. after |

**Path to production (process note, not built in v1)**

1. The one number v1 computes is the dry-run error rate by field, from ground truth. It makes the case for the human gate.
2. In production, a field would become eligible for auto-write when the Wilson upper bound of its error rate in the top confidence bucket falls below a risk-tiered target at a minimum n, with a 24-hour undo posted in Slack. v1 data is too small to reach that n.
3. Confidence should come from agreement across samples or from logprobs, not the model's self-reported score, which is poorly calibrated.
4. Reviewer-derived error rates are a floor, because approvals can be wrong. The gap to ground-truth error is the case for a production audit sample.
5. Edits and rejects are separate error types: a reject means no proposal should exist, an edit means right direction with a wrong value.

**Call volume.** Roughly 1,500 extraction calls: 5 noise-floor runs and 8 curve snapshots on 40 validation transcripts, candidate-rule replays, the manual baseline, and the test run, plus rule-learner calls. Check them against the credit budget in Section 7 before the batch run. A small pinned model and a cap on candidates per batch keep it in range.

## 7. Build order, cut order, and risks

The build runs in eight milestones, with a hardcoded vertical slice first and the eval baseline before the full Slack loop, because an extractor that cannot be measured makes everything built on it decoration. The slice de-risks the integrations and makes something demoable by about evening 2. Effort is a rough estimate in evenings.

| # | Milestone | Exit criterion | Effort (evenings) |
| --- | --- | --- | --- |
| M0 | Setup: Slack app, HubSpot service key, seed script with --reset, 15 seeded fictional deals, OpenAI key, repo, SQLite schema, plus a hardcoded vertical slice | A hardcoded proposal is approved in Slack and lands in HubSpot, and --reset restores it. Verify free-tier limits, pick the OpenAI model, and check its prices. | 1-2 |
| M1 | Data: deals.json (100 fictional deals with truth values, traps, and house rules) from a seeded script, transcripts via request and ingest, verifier pass, parser into interactions and utterances | Rendering error rate measured | 1-2 |
| M2 | Extractor, validator, dry-run eval, noise floor, manual baseline | Baseline V0 and manual-baseline numbers. If F1 on trap cases is above about 95%, the traps are too easy: harden them. House-rule cases should fail at V0 by design. | 2 |
| M3 | Slack review loop: folder-watcher ingest, cards, modals, click authorization, stale check, HubSpot writeback, logging | One transcript live end to end | 2 |
| M4 | Learning loop: rule learner, versions, candidate to validate to activate, few-shot, revert | Reject, then rule, then changed output on a paired transcript | 2 |
| M5 | Batch experiment: oracle reviewer, learning curve, ablation, test run, about 10 hand-reviewed | The results table | 1-2 |
| M6 | Static results charts and unit-economics table. Stretch: weekly digest and escalation timers. | Charts render in the README | 1 |
| M7 | Write-up: README, about 2 minute demo recording, results with counts, limitations | Publishable | 1 |

**Credit budget.** OpenAI credits (about $20) cover the extractor and the rule learner across roughly 1,500 calls. Transcripts and verification run on the Claude subscription and cost no API credits; they count against plan usage limits. Confirm OpenAI prices and per-run spend in M0, and track spend per run.

**If time gets tight, cut in this order:** the optional contamination check, then the ablation, then chart polish (keep the validation-score chart). Timers and the digest are cut by default.

**Never cut:** the baseline eval, the manual baseline, the validator, rule versioning, the noise floor.

**Risks**

- **Model or pricing changes, or credit overrun:** pin the model, keep the extractor behind one interface, and track spend per run.
- **HubSpot custom-property caps:** check in M0.
- **Socket Mode needs the laptop running for demos:** rehearse the paired-trap demo and keep a recorded fallback.
- **A live model may not fail on cue:** the paired transcripts are engineered with the same trap, rehearsed, with the recording as backup.
- **Oracle-only results overstate learning:** the hand-reviewed run and the explicit human-vs-oracle gap report address it.
- **Transcript rendering errors:** a truth is missing, too plain, or mis-cited. The verifier pass and the rendering audit catch it.

## 8. Decision log

Twenty-four decisions were settled or revised during scoping and the Oct 1 review, and this spec reflects the final choice for each. Anything marked open is unverified or unconfirmed.

| Topic | Final decision | Replaced |
| --- | --- | --- |
| Review surface | Slack channels, with owner-only click authorization | DMs to the record owner |
| Review channel name | interaction-to-crm-updates-review | interaction-to-crm-updates-dm |
| Extractor model | OpenAI, model pinned (Claude generates transcripts) | Gemini free tier |
| Data split | 100 transcripts: 40 learn, 40 validation, 20 test | 60 transcripts: 30 learn, 15 validation, 15 test (originally 50: 30 learn, 20 holdout) |
| Case mix | About 80% of transcripts carry 2–3 trap or house-rule cases, about 20% clean | One trap in about 40% of transcripts |
| House rules | 4–5 company conventions the extractor is never told, enforced by the oracle | Traps only |
| Transcript generation | Truth-first: true values in deals.json, Claude renders them on the subscription (Claude Code agent or chat), a verifier agent checks each | A Claude API script; before that, the generator wrote the transcript, then the key |
| Company pools | Fictional companies only, justified by publishing ethics. Contamination is handled by the evidence check. Real companies belong to the signal-trigger project. | Two pools in this spec, fictional justified by contamination risk |
| Learning-curve baseline | V0 and a 1-hour manual prompt. The loop must match or beat the manual prompt. | V0 only |
| Noise floor | 5 runs, per-instance flip rate, paired test on changed instances | 3 runs, largest field-level spread |
| Rule activation | Candidate, validate on 40 transcripts against the noise floor, then activate. Demo mode activates immediately, flagged unvalidated. | Validate on 15 transcripts (originally auto-activate on rejection) |
| Cost metric | Dry-run error rate by field. Auto-write readiness is a process note, not built. | Counterfactual auto-write error rate with Wilson bounds and a readiness panel |
| Negated values | Proposed only when they change what the CRM says | Open item |
| Stage signal | Logged as a deal note, never written to deal stage | Open item |
| Decision timeline | Custom property | Native close date |
| Evidence check | Exact substring after normalizing whitespace, quote marks, and punctuation | Raw exact substring |
| Pain points and use case scoring | Taxonomy enum, exact match | Free text with an LLM judge |
| Hedged values | Proposed with a tentative tag | Suppressed |
| Batch reviewer | Oracle reviewer enforcing traps and house rules, plus about 10 hand-reviewed transcripts, with a gap report | Hand review of every proposal |
| Build order | Hardcoded vertical slice in M0, eval baseline (M2) before the full Slack loop (M3) | Slack first |
| Ingest | `python -m crm ingest` validates and files transcripts from `inbox/`; `--watch --run` is the live trigger standing in for a call-recorder webhook | No trigger specified |
| HubSpot seeding | Idempotent script with --reset, about 15 live deals, the other 85 frozen as snapshots in the keys | Seeding method not specified |
| Results display | Static charts in the README | Streamlit dashboard |
| Timers and digest | Stretch goals, cut by default | First in the cut order |

**Open items**

- The channel name `-review` replaces `-dm` on my recommendation. Rename it if you prefer the original.
- HubSpot custom-property limits, record caps, API scopes, Slack free-plan limits, and OpenAI model prices are unverified, as is whether the OpenAI budget covers about 1,500 extraction calls. Check them in M0.
- The house-rule list in Section 2 is a draft. Confirm the final 4–5 before M1.
- Numeric targets for F1 and error rate are set after the baseline run, not before.
- Real (non-synthetic) transcripts are out of scope for v1. A small real sanity check was floated and not committed.
