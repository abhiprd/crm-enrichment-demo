# Demo runbook (about 2 minutes)

Shows one loop live: a call produces a Slack card, a reviewer rejects a proposal, a rule is learned, and the next call changes. All companies and people are fictional. Needs `.env` with HubSpot, Slack and OpenAI keys, and the laptop running (Socket Mode). Use `.venv/bin/python` if the system Python lacks the dependencies.

## Before recording (not on camera)

```bash
python3 -m crm preflight                                # keys, scopes, models
python3 -m crm rules list                               # find the version whose active rules are empty (v5 at the time of writing)
python3 -m crm rules revert 5                           # back to that empty ruleset (a new version; nothing is rewritten)
python3 scripts/seed_hubspot.py --demo --live --reset   # demo deal back to its starting values
```

Rehearse once end to end. The paired calls are engineered with the same trap (a vendor mentioned only as another company's tool), but a live model may not fail on cue: keep a recorded take as the fallback.

## On camera

| Time | Step | Command or action | Say |
| --- | --- | --- | --- |
| 0:00 | Start the watcher | `python3 -m crm ingest --watch --run --live --force` | "A transcript dropped in this folder stands in for a call recorder's webhook." |
| 0:10 | Drop call A | `cp prompts/demo/pair_a.md inbox/demo-pair-a.md` | "One card per call; every proposal shows the quote, speaker and time." |
| 0:30 | Reject the competitors row | Reject, reason `not_crm_worthy` | "The competitor was another company's tool, not an evaluation. I reject it." |
| 0:45 | Show the rule | the thread reply in Slack | "A second model drafts a general rule from the reject. It is in force right away, flagged unvalidated, and replayed on the validation set in the background." |
| 1:05 | Drop call B | `cp prompts/demo/pair_b.md inbox/demo-pair-b.md` | "A different company, the same trap." |
| 1:20 | Show the new card | competitors row | "In rehearsal the trap vendor was not proposed and the genuine evaluation still was. That is two fictional calls, no statistics." |
| 1:40 | Show the gate and revert | `python3 -m crm rules list`, then `python3 -m crm rules revert <version>` | "Every rule is versioned and revertible. Say what the gate shows; if it has not finished, say it is still running. The earlier rules for this trap failed the gate." |
| 1:55 | Approve a row | Approve one proposal | "Approve writes to HubSpot only if the CRM value has not changed since the proposal." |

## After

```bash
python3 scripts/seed_hubspot.py --demo --live --reset   # restore the demo deal (review notes are not deleted)
```

## What not to claim on camera

The learned rules did not clear the validation gate in M4, and in M5 only three of eleven candidates did. The demo shows the mechanism and one changed output on a paired call. It does not show a validated improvement; the measured results are in the README and `STATUS.md`.
