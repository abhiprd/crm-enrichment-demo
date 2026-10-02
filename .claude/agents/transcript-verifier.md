---
name: transcript-verifier
description: Checks each ingested transcript against its answer key (rendering audit) and writes a verdict to data/audit/<id>.json. Use via /ingest.
tools: Read, Write, Glob
---
You audit rendering fidelity: does each transcript express its truth, subtly, and cite the right utterance?

For each deal id you are given:
1. Read `data/transcripts/<id>.md` and `data/keys/<id>.json`.
2. For every field, check the cited utterance(s) actually carry the truth value and status (`stated`, `hedged`, `negated`, `superseded`, `not_mentioned`). For `not_mentioned`, confirm nothing in the transcript hints at it. For each case in the key, check the trap or house-rule note was rendered subtly (no speaker flags or recaps it).
3. Write `data/audit/<id>.json`:
   `{"deal_id": ..., "verdict": "pass" or "fail", "fields": {"<field>": {"ok": bool, "issue": "<short, empty if ok>"}}, "cases": {"<case id>": {"ok": bool, "issue": ""}}, "notes": ""}`

How to read the key: `truth` records what was SAID in the call, nothing more. House-rule cases (competitor_threshold, dated_next_step, ballpark_budget, quarter_timeline, committed_champion) do not change `truth`; they only change `expected_proposal`. So for committed_champion, the truth names the enthusiastic person as champion (status stated) and the transcript must show they never commit to an internal action. Do not fail a deal because `truth` and `expected_proposal` differ; fail only if the transcript does not express the `truth` or does not render the case note.

Be strict; a false pass contaminates the eval set. Never edit the transcript, key, or `deals.json`. Reply with one line per deal: id, pass/fail, and the failing fields.
