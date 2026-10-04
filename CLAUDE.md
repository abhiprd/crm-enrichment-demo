# Interaction-to-CRM Updates

Call transcript in, proposed HubSpot deal updates out, reviewed in Slack, with a learning loop whose improvement is measured against a noise floor. Portfolio project proving GTM Engineer skills. Read `PLAN.md` first, then the relevant section of `docs/SPEC.md`. Where they conflict, PLAN.md wins. Current progress is in `STATUS.md`.

## Stack

Python 3.9+, SQLite, Pydantic, slack-bolt (Socket Mode), HubSpot REST (service key), OpenAI for the extractor and rule learner. Transcripts are written by Claude through the subscription (Claude Code agent or claude.ai chat), never the Anthropic API.

## Layout

- `crm/` package. CLI: `python -m crm <ingest|request|status>`. Later milestones add `extract`, `eval`, `slack`, `seed`.
- `data/deals.json` the single source of truth for the eval set. `data/taxonomy.json` enums.
- `data/transcripts/<id>.md` call metadata and utterances only. `data/keys/<id>.json` answer keys. `data/audit/<id>.json` verifier verdicts.
- `inbox/` drop zone for generated transcripts. `inbox/processed/`, `inbox/rejected/` (with `.errors.txt`).
- `prompts/transcript_request.md` generator prompt template. `prompts/requests/` rendered requests.
- `tests/` pytest. Run `python -m pytest -q` before every commit.

## Commands

- `python -m crm status` dataset progress by split, plus deal validation errors.
- `python -m crm request --n 5 [--split learn] [--deals d001,d002]` render the next transcript request.
- `python -m crm ingest [--force] [--file PATH]` validate and ingest everything in `inbox/`.
- `python -m crm ingest --watch --run` live-demo mode: watch `inbox/`, send each call to the pipeline.

## Hard rules

1. **Eval isolation.** From M2 on (once the extractor exists), the main session never opens `data/keys/*.json`, `data/audit/*.json`, or the `fields` of validation or test deals in `data/deals.json`. Only the transcript-writer and transcript-verifier agents and Python code read them. When working on extractor prompts or rules, look only at learn-split transcripts and keys.
2. **Test set runs once.** Never run anything on the test split except the single final run in `/eval-run test`. If it has already run (`results/test_run.json` exists), stop and ask.
3. **Truth is fixed.** Never edit `deals.json` truth, keys, or the taxonomy to make a score or a test pass. If the truth looks wrong, say so and ask.
4. **No fabricated results.** Every number in README, STATUS.md, or a commit message comes from a file under `results/` produced by code in this repo. Report counts with denominators.
5. **Noise floor gates claims.** A change is a result only if it clears the measured noise floor (SPEC Section 6). Otherwise call it "within noise".
6. **No Anthropic API.** Do not add the `anthropic` SDK or use `ANTHROPIC_API_KEY`. If that variable is set, Claude Code bills the API instead of the subscription.
7. **One model interface.** All OpenAI calls go through `crm/llm.py`, with the model pinned in config and spend logged per run.
8. **Dry-run by default.** Anything that writes to HubSpot or posts to Slack needs an explicit `--live` flag.
9. **Fictional companies only.** `.example` domains, invented people. Real vendor names appear only as competitors.
10. **Secrets** live in `.env` (never committed, never printed). `.env.example` lists the keys.

## How to work

- One milestone at a time with `/milestone Mx`. Plan before code on M2 to M4. Finish by meeting the exit criterion, updating `STATUS.md`, and committing.
- New transcripts: `/transcripts`. Ingest and verify: `/ingest`. Experiments: `/eval-run`.
- Before any result reaches README or STATUS.md, have the `eval-auditor` agent check it.
- Before every commit, run the `infosec-reviewer` agent (`.claude/agents/infosec-reviewer.md`) on the staged diff and fix or report its findings before committing. If that file is missing, stop and ask. The git hook `.githooks/pre-commit` (`scripts/precommit_scan.py`, enabled with `git config core.hooksPath .githooks`) also blocks secrets, personal data, and private eval files; never bypass it with `--no-verify`.
- Keep functions small and typed. Add a test with every behavior change. Prefer the standard library until a milestone needs a dependency, then add it to `pyproject.toml`.
- Ask before: adding a dependency, changing the data contract in `crm/schema.py`, or anything in Hard rules.
