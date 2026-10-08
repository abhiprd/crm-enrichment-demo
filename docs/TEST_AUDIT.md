# Test-suite audit

Date: 2026-10-08. Numbers are in `results/test_audit.json`; they come from mutmut's per-mutant results and from `pytest --cov --cov-branch`, both run on this repository. This is a measurement of how well the tests detect changes, not a claim that the suite is comprehensive.

## What was run

| Check | Tool | Scope |
| --- | --- | --- |
| Mutation testing | `mutmut` 3.8.0, one fresh run per module | 11 modules: `scoring`, `propscore`, `proposals`, `validator`, `stats`, `review`, `rules`, `gate`, `extractor`, `learner`, `oracle` (3,876 mutants) |
| Branch coverage | `pytest --cov=crm --cov-branch` | all of `crm/` |
| Test names | `pytest --collect-only`, read by hand | all tests |
| Tautology hunt | grep for `monkeypatch`/mocks and a read of each flagged test | all tests |

**Not mutated:** `slack_app`, `handreview`, `slice`, `cli`, `ingest`, `pipeline`, `curve`, `evalrun`, `evalcmds`, `bakeoff`, `charts`, `hubspot`, `llm`, `preflight`. The first four are excluded in `pyproject.toml` because they have little or no test coverage, so every mutant would survive and tell us nothing new. The rest were not reached in this pass, not judged safe.

## Headline

| | Before | After |
| --- | --- | --- |
| Tests | 133 | 288 (+155 in four new files) |
| Mutants detected / total (11 modules) | 2,567 of 3,876 (66.2%) | 3,295 of 3,876 (85.0%) |
| Mutants that survived | 1,273 | 554 |
| Mutants with no covering test | 36 | 27 |
| Line and branch coverage of `crm/` (statements missed) | 71% (849 of 3,077) | 76% (700 of 3,077) |

"Detected" means the mutant was killed or aborted the test run (four aborts, all in `extractor.extract`). Equivalent mutants are not removed from the denominator, so 85.0% is a floor.

Per module (detected / mutants, before then after):

| Module | Before | After |
| --- | --- | --- |
| `scoring` | 239 / 351 (68.1%) | 338 / 351 (96.3%) |
| `propscore` | 155 / 180 (86.1%) | 167 / 180 (92.8%) |
| `proposals` | 265 / 317 (83.6%) | 308 / 317 (97.2%) |
| `validator` | 61 / 74 (82.4%) | 70 / 74 (94.6%) |
| `stats` | 140 / 177 (79.1%) | 163 / 177 (92.1%) |
| `review` | 509 / 1019 (50.0%) | 747 / 1019 (73.3%) |
| `rules` | 242 / 325 (74.5%) | 275 / 325 (84.6%) |
| `gate` | 316 / 488 (64.8%) | 399 / 488 (81.8%) |
| `extractor` | 216 / 290 (74.5%) | 260 / 290 (89.7%) |
| `learner` | 234 / 381 (61.4%) | 324 / 381 (85.0%) |
| `oracle` | 190 / 274 (69.3%) | 244 / 274 (89.1%) |

Line coverage did not predict mutation strength. Before the new tests `scoring` was already 89% covered by lines, yet 112 of its 351 mutants survived: the existing tests ran the code without asserting the results.

## What the survivors showed

Before the new tests, the weakest spots were in code that produces the project's headline numbers:

- Flipping the economic-buyer and champion comparison survived, so no test checked that a wrong person scores wrong.
- The noise-floor rule survived changes to `changed > noise_flips` and `p < 0.05`.
- McNemar's `n = improved + worsened`, the bootstrap interval bounds and the `compare` thresholds survived.
- Competitor F1, budget ranges, quarter boundaries and next-step scoring survived arithmetic changes.
- `Proposal` fields (`field`, `prop`, `current`, `status`) were never asserted, only the proposed text.
- The ruleset version metadata (parent, reason, author) and the validation scores on version rows were never asserted.

None of this changes a past result, because the code was not modified. It means a regression in those functions would have gone unnoticed. The new tests assert each of these.

### What still survives (554 mutants)

| Kind | Count | Assessment |
| --- | --- | --- |
| Message and label text | 157 | Card text, button labels and error strings. Low value; not asserted line by line. |
| String casing | 205 | Mostly UI labels and dictionary keys changed to other casing. Not inspected one by one, so a few may be real gaps (an `action_id` in a different case would matter). |
| SQL keyword casing | 80 | Equivalent: SQL is case-insensitive. |
| File encoding argument | 6 | Equivalent (`"UTF-8"` vs `"utf-8"`, or the platform default). |
| Logic | 106 | Real gaps, down from about 615 before. Largest clusters: `gate.validate_rule` argument pass-through (23), `oracle.review_extraction` (12), `learner.learn_from_reject` (8), `stats.compare` and `paired_bootstrap` defaults (10), `review.modal_values` (6). |

## Branch coverage

After: 76% overall. Modules at or near 100% now: `scoring`, `stats`, `validator`, `propscore`, `learner`, `oracle`, `extractor`, `review` (99%), `rules` (99%), `proposals` (99%), `hubspot` (96%, from 61%), `gate` (97%).

Still low, and not covered by this pass:

| Module | Coverage | What is untested |
| --- | --- | --- |
| `slack_app` | 0% | All handlers (needs live Slack) |
| `__main__` | 0% | Entry point |
| `curve` | 35% | `run_curve`, `learn_batch`, `review_batch`, `run_ablation`, `run_test` happy path |
| `slice` | 33% | The M0 vertical slice |
| `cli` | 41% | `cmd_ingest`, `cmd_eval`, `cmd_handreview`, `cmd_charts`, `cmd_preflight` |
| `preflight` | 42% | Slack and OpenAI checks; a network error on the HubSpot probe |
| `bakeoff` | 52% | Run and selection paths |
| `handreview` | 60% | `build`, `post`, `serve`, `frozen_template` |
| `ingest` | 84% | `watch()` and partial validation branches |
| `pipeline` | 78% | The live path (Slack user lookup, live card posting) |

## Test names and failure cases

By name, about 95 to 100 of the original 133 tests were happy-path or pure-function checks and about 30 to 35 covered a failure case; this is an estimate from reading names, not an exact count. The 155 new tests are mostly failure and boundary cases. They add, among others:

- **Empty transcript:** `parse_transcript("")` and a prompt built from zero utterances.
- **Malformed LLM JSON:** prose-wrapped JSON, fenced JSON, no braces, non-dict JSON, bad statuses and evidence shapes, and both retry attempts unparseable.
- **API timeout and rate limit:** each of `RateLimitError`, `APIConnectionError`, `APITimeoutError` and `InternalServerError` retried; the 60-second cap; six attempts by default; a non-transient error raised at once; a missing API key.
- **CRM errors:** HubSpot 300, 400, 401, 404, 429, 500 and 503 raise `HubSpotError`; a missing token; reads in dry-run; the account and owner lookups swallowing errors.
- **Review edge cases:** a missing proposal, a second reject, edit or approve on a closed proposal, an empty edit, a note that must not trigger a stale check, numerically equal CRM text.
- **Duplicate record:** the same call processed twice (see findings).

## Tautologies

No test mocks a function and then asserts only on the mock's return value. The suite uses hand-written fakes and `monkeypatch` in only a few places. Three weak assertions were found and addressed:

- `tests/test_learning.py:58` asserted `net == 4`, which the stub itself supplies. A new test makes the stub return different nets per field and asserts that the rule's own field is the one reported.
- `tests/test_evalcmds.py:115` asserted `learn_errors(...) == []`, which would pass if the function always returned an empty list. A new test adds a learn-split deal and asserts the entries.
- `tests/test_extractor_scoring.py:25` asserted `prompt_version() == prompt_version()`, which asserts nothing. A new test pins the format and that the hash follows the text.

## Findings in the code (not fixed: no source file was changed)

1. **Next-step scoring gives date credit to an empty prediction.** When the prediction is junk or missing and the key's date is null, `None == None` earns the date part, so an empty prediction scores 1/3 on `next_step`. This sits in the scorer behind the reported results; changing it would change past numbers, so it needs a decision. `tests/test_scoring_stats_gaps.py` pins the current behaviour.
2. **A stray comma in few-shot examples with no quote.** `compose_template` renders `- budget,: a reviewer changed...`. Cosmetic, but it is in the prompt the extractor sees.
3. **No deduplication when a call is processed twice.** Each run makes a new interaction and a new set of pending proposals. Two runs within the same second collide on the interaction id (a timestamp to the second) and the second run fails with a database integrity error.
4. **Validation scores are not stored on the version row when a candidate rule passes the gate.** `apply_verdict` records the scores before it activates the candidate, so the version created by the activation has none. They land only for rules that were already active (demo mode).
5. **No 429 handling in the HubSpot client.** A rate-limited request raises `HubSpotError` and is not retried; only the OpenAI layer retries.

## How to reproduce

mutmut forks one worker process per mutant. On this Mac, Apple's bundled Python 3.9 crashed in those workers, and macOS showed a "Python quit unexpectedly" notice for each one. Python 3.12 from Homebrew ran cleanly with no crash reports, so the mutation runs used a throwaway virtual environment outside the repository.

```bash
/opt/homebrew/bin/python3.12 -m venv /tmp/mmvenv
/tmp/mmvenv/bin/pip install slack-bolt requests python-dotenv openai pytest pytest-cov mutmut
/tmp/mmvenv/bin/mutmut run "crm.scoring*"
```

Run one module at a time and read `mutants/crm/<module>.py.meta` for the per-mutant exit codes. The `[tool.mutmut]` block in `pyproject.toml` copies `prompts/`, `data/` and `scripts/` into the scratch `mutants/` directory (git-ignored) because `crm/extractor.py` and `crm/learner.py` resolve those paths from the package's parent directory. `data/` includes the private answer keys, so `mutants/` must never be committed or shared, and it is removed after each audit. Branch coverage runs on the project's own `.venv`:

```bash
.venv/bin/python -m pytest --cov=crm --cov-branch --cov-report=term-missing -q
```

## Recommended next steps

- Decide whether to fix findings 1 and 3, which affect results or crash a re-run.
- Mutation-test the modules not reached here, starting with `pipeline`, `ingest`, `hubspot`, `llm` and `evalrun`.
- Cover the `curve` and `cli` entry points with fake-client tests; they are the largest untested code that runs the experiments.
- Review the 106 remaining logic survivors, beginning with `gate.validate_rule` argument pass-through.
