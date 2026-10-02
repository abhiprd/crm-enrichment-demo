# Build plan

This plan turns `docs/SPEC.md` into work for Claude Code. It overrides the spec in one place: transcripts come from Claude on your subscription, not the Anthropic API.

## What changed from the spec (Oct 1)

| Topic | Now | Was |
| --- | --- | --- |
| Transcript generation | Claude on the subscription: the `transcript-writer` agent in Claude Code (default) or a claude.ai chat. Both drop files into `inbox/`. | A Claude API script |
| Verifier pass | `transcript-verifier` agent in Claude Code | An API call |
| Claude credit budget | $0. Usage counts against your plan's limits, shared between chat and Claude Code. | About $8–10 |
| Ingest | `python -m crm ingest` validates and files every transcript, from either path. `--watch --run` is the live-demo trigger. | Folder watcher only |
| Deals | `deals.json` is produced by a seeded script (`scripts/make_deals.py`, M1), then reviewed. | Hand-written or LLM-written |

The model separation still holds: Claude writes transcripts, OpenAI extracts from them.

**Why the agent path is the default.** Chat works, but each batch is a paste in, copy out, save file loop. The agent reads the request file and writes straight to `inbox/`, on the same subscription. Use chat when you want to watch a transcript being written or run a live demo.

**Check before generating:** if `ANTHROPIC_API_KEY` is set in your shell, Claude Code bills the API instead of your plan. Unset it.

## The ingest command

```
python -m crm request --n 5 --split learn     # writes prompts/requests/req_<time>_<ids>.md
# agent path: /transcripts runs the transcript-writer on that file
# chat path:  paste the file into claude.ai, save the reply into inbox/
python -m crm ingest                          # validates, files, moves the source file
python -m crm status                          # progress by split
```

Each inbox file holds one or more `=== DEAL <id> === ... === END <id> ===` blocks (format in `prompts/transcript_request.md`). Code fences and chat chatter outside blocks are ignored. For every block, ingest checks:

- the deal exists, and date, call type, and participants match `deals.json`
- every line parses as `[idx] [mm:ss] Speaker (Role): text`, idx runs 0..n-1, time never goes backwards, every speaker is a participant, at least 10 turns
- no case id, status name, or "answer key" style text leaks into the dialogue
- `EVIDENCE` cites at least one valid idx for every mentioned field and nothing for `not_mentioned` fields

Valid blocks become `data/transcripts/<id>.md` and `data/keys/<id>.json` (truth copied from `deals.json`, plus the cited idx). A file with any failure moves to `inbox/rejected/` with an `.errors.txt`; its valid blocks are still filed, so you regenerate only what failed. Existing transcripts need `--force`, and replacing one deletes its old audit verdict.

Live demo: a chat transcript with a `demo-` id (for example `demo-northwind`) needs no deal or evidence. `python -m crm ingest --watch --run` picks it up and, once M3 exists, sends it to the pipeline.

## Decisions to confirm before M1

| # | Decision | Recommendation |
| --- | --- | --- |
| D1 | How house rules are scored | Keys carry `truth` (what was said) and `expected_proposal` (what should land in the CRM after house rules). Extraction F1 scores `truth`. The house-rule learning curve scores proposals against `expected_proposal`. House rules apply to every deal's `expected_proposal`, not only deals labeled with that case. The case label marks the deals built to trigger it. |
| D2 | Default generation path | Agent. Chat stays available. |
| D3 | Deals per request | 5. Long enough to be efficient, short enough that a chat reply doesn't truncate. |
| D4 | Taxonomy | `data/taxonomy.json` draft: 8 pain points, 8 use cases, 8 next-step actions. Edit before M1, then freeze. |
| D5 | House-rule list | The draft five in `crm/schema.py`. Cut to four if one proves hard to render subtly. |

`data/deals.json` currently holds three example deals that follow D1. They are the format reference for `make_deals.py`.

## Milestones

| # | Goal | In Claude Code | Exit criterion |
| --- | --- | --- | --- |
| M0 | Accounts, keys, SQLite schema, `scripts/make_deals.py` (pulled forward from M1), HubSpot seed script with `--reset`, and a hardcoded vertical slice | `/milestone M0` | A hardcoded proposal is approved in Slack, lands in HubSpot, and `--reset` restores it |
| M1 | 100 transcripts and the rendering audit (`make_deals.py` already built in M0) | `/milestone M1`, then `/transcripts` and `/ingest` in batches | `crm status` shows 100 verified. Hand-check 15; rendering error rate under about 5% |
| M2 | Extractor, quote validator, dry-run eval, noise floor, manual baseline | `/milestone M2` (plan first), `/eval-run noise`, `/eval-run baseline` | `results/v0_noise.json` and `results/manual_baseline.json` exist |
| M3 | Slack review loop and HubSpot writeback, fed by `ingest --watch --run` | `/milestone M3` | A demo transcript dropped in `inbox/` produces a card; approve writes to HubSpot |
| M4 | Rule learner, versions, validation gate, few-shot, revert | `/milestone M4` (plan first) | Reject, then rule, then changed output on a paired transcript |
| M5 | Learning curve, ablation, the single test run, about 10 hand-reviewed calls | `/eval-run curve`, `/eval-run ablate`, `/eval-run test`, `eval-auditor` | `results/` holds the curve, ablation, test run, and human-vs-oracle gap |
| M6 | Static charts and the unit-economics table | `/milestone M6` | Charts render in the README |
| M7 | README, 2-minute demo recording, limitations | `/milestone M7`, `eval-auditor` on every claim | Publishable |

## Working agreement

- One milestone per session. Start with `/milestone Mx`, end by updating `STATUS.md` and committing.
- Use plan mode for M2, M3, and M4: those set the data model the rest depends on.
- Generate transcripts in batches of 5 to 10. 100 transcripts plus verification may span more than one usage window on Pro.
- After each batch, run `/ingest`. Fix rejects before generating more, so prompt problems surface after 5 calls instead of 50.

## First session

1. `git init`, create the GitHub repo, commit this scaffold.
2. `python -m pytest -q` (20 tests should pass).
3. Confirm D1 to D5, or tell Claude Code what to change.
4. Run `/milestone M0`.
