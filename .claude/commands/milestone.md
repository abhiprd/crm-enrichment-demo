---
description: Work one milestone (M0-M7) from PLAN.md
argument-hint: Mx
---
Work milestone $ARGUMENTS.

1. Read `PLAN.md`, then the matching section of `docs/SPEC.md` (Sections 4-7), then `STATUS.md`. PLAN.md wins on conflicts.
2. State the milestone's exit criterion and what is already done.
3. For M2, M3, M4: enter plan mode and get approval before writing code.
4. Build in small typed functions, with a test for every behavior change. Prefer the standard library. Ask before adding a dependency (add it to `pyproject.toml` only after approval).
5. Respect CLAUDE.md hard rules, especially eval isolation (M2 on), dry-run by default, and no Anthropic API.
6. Finish only when the exit criterion is met: run `python3 -m pytest -q`, update `STATUS.md` (every number cites a file under `results/`), and commit.

If the exit criterion cannot be met, say what is blocking and stop. Do not weaken it.
