# Interaction-to-CRM Updates

Turn a sales call transcript into proposed HubSpot deal updates, have a human approve them in Slack, and measure how well the extraction holds up.

```
call transcript  ->  extractor (LLM)  ->  quote check  ->  proposals  ->  Slack review card  ->  HubSpot
                                                                |
                                                  approve / edit / reject
```

Everything runs in dry-run by default. Posting to Slack or writing to HubSpot needs an explicit `--live` flag.

## What it can do

- **Extract nine CRM fields from a call**: budget, decision timeline, competitors (with stance), economic buyer, champion, pain points, use case, next steps, and a stage signal. Each value carries a status (stated, hedged, negated, superseded, or not mentioned) and the quote it came from.
- **Reject hallucinations deterministically**: a value is only proposed if its quote appears in the cited utterance.
- **Propose, never overwrite**: extractions are diffed against the deal's current HubSpot values. Hedged values are marked tentative, negated competitors become `ruled_out`, lists are merged without duplicates, and the stage signal becomes a deal note, never a stage change.
- **Review in Slack**: one card per call with every proposal, the quotes behind it (speaker and timestamp), a link to the deal, and Approve / Edit / Reject on each field. Only the record owner or a manager can act. There is no approve-all button.
- **Write back safely**: the CRM value is re-read before each write; if it changed since the proposal, the card refreshes instead of writing. Double clicks are ignored.
- **Watch a folder**: drop a demo transcript in `inbox/` and `ingest --watch --run --live` posts the card.
- **Seed a sandbox**: an idempotent script creates fictional companies and deals in HubSpot (with a reset), and `crm preflight` checks keys, scopes, models, and Slack tokens before any live step.
- **Measure extraction quality**: 100 synthetic transcripts with answer keys, split 40 learn / 40 validation / 20 test. A run-to-run noise floor, a manual prompt baseline, and paired statistics tell real improvements apart from noise.
- **Log everything** to SQLite (interactions, extractions, proposals, review decisions) and track model spend per run.

All companies and people are fictional (`.example` domains). Competitor names are real vendors, as reps actually say them.

## Quick start

Python 3.11+ recommended.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env        # add your keys; never commit .env
python3 -m pytest -q
python3 -m crm status       # dataset progress by split
python3 -m crm preflight    # verify keys, scopes, and models before live steps
```

Live demo (needs HubSpot, Slack, and OpenAI keys in `.env`):

```bash
python3 scripts/seed_hubspot.py --demo --live          # create the demo deal
python3 -m crm ingest --watch --run --live --force     # watch inbox/, post cards to Slack
cp prompts/demo/northwind.md inbox/demo-northwind.md   # drop a call; a card appears in Slack
python3 scripts/seed_hubspot.py --demo --live --reset  # restore the demo deal afterwards
```

Evaluation (dry-run plan first; add `--run` to call the model):

```bash
python3 -m crm eval noise [--run]       # V0 noise floor on the validation split
python3 -m crm eval baseline [--run --approved]
```

## Repository layout

| Path | Contents |
| --- | --- |
| `crm/` | The package: pipeline, extractor, validator, proposal builder, review logic, Slack and HubSpot clients, eval harness, CLI |
| `data/` | Deals and answer keys, taxonomy and label definitions, transcripts, audit verdicts |
| `prompts/` | Extractor prompts, transcript generator prompt, the demo transcript |
| `scripts/` | Deal generator, HubSpot seeding, hand-check worksheets |
| `results/` | Eval outputs (noise floor, baseline, bake-off) |
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

**To do**

- **M4, Learning loop**: turn rejects into candidate rules and edits into examples, gate each rule on the validation set, versioned and revertible rulesets.
- **M5, Experiments**: learning curve, ablations, the single test-set run, and a human-versus-oracle reviewer comparison.
- **M6, Charts and unit economics**: static charts and a cost and latency table.
- **M7, Write-up and demo**: final write-up, a short demo recording, and a limitations section.

Measured results and per-milestone evidence are tracked in [STATUS.md](STATUS.md).
