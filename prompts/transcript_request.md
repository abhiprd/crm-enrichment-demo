# Transcript request: {{DEAL_IDS}}

You are writing synthetic B2B sales call transcripts for an evaluation set. A separate system will later read each transcript and try to extract CRM facts. Your job is to render the facts below into realistic calls so that a careful reader could recover them, and a careless one would make the mistakes each case is designed to provoke.

## Vendor (the internal side of every call)

{{VENDOR}}

## Deals to render

{{DEALS_JSON}}

## How to read each deal

- `call`: date, type (discovery, demo, negotiation) and `target_turns`. Use that date and type exactly.
- `participants`: use exactly these people, names, roles and orgs. Add no one.
- `facts`: one entry per CRM field with the true `value` and `status`.
  - `stated`: said plainly, once or more.
  - `hedged`: said, but without commitment ("probably", "if things go well", "we're hoping").
  - `negated`: explicitly ruled out ("we're not looking at X anymore").
  - `superseded`: an earlier value is said first, then changed later in the call. The `value` is the final one. Say the earlier one too.
  - `not_mentioned`: never discussed. Not hinted at, not asked about, not answered. If a speaker would naturally ask about it, steer the conversation elsewhere.
- `cases`: each case names a field and carries a `note` saying what must happen in the call. Follow the note exactly and subtly. No speaker comments on it, recaps it, or flags it.

## Writing rules

1. Natural speech: small talk, filler, interruptions, tangents, half-finished thoughts. No tidy recap at the end unless the note asks for one.
2. Facts arrive indirectly and scattered, as real people say them. Numbers in spoken form ("about fifty grand", "low six figures").
3. People only know what they would know. The internal rep never states the prospect's facts for them.
4. Never use these words in the dialogue: any case id or status name from the data above, "answer key", "ground truth", "house rule", "REVEAL", "EVIDENCE".
5. Timestamps start at 00:00 and only move forward. Pace roughly 15 to 30 seconds per turn.
6. Hit `target_turns` within about 20%.

7. A `not_mentioned` field must leave no trace, including indirect hints. Check each one against the whole call:
   - Vendor demos and pitches are the usual leak. When a field is `not_mentioned`, the rep does not demo, pitch, or ask about its topic, and the prospect does not react to it. For `use_case`, only show or discuss the listed use cases; do not demo adjacent capabilities (coaching, forecasting, call libraries, recording, deal inspection) that map to any other use-case value.
   - For `pain_points`, the prospect voices only the listed pains. A complaint that would map to any other pain value (stale data, blank fields, slipping deals, slow ramp, tool sprawl) must not appear, even in passing.
   - Likewise for budget, timeline, competitors, buyer, and champion: no hints, no "we'll figure that out later".
   Small talk and logistics are fine. Before writing the final block, re-read the dialogue once per `not_mentioned` field and delete or rewrite anything that touches it.

## Output format (exact)

Output one block per deal, in the order given, and nothing else. No commentary before, between, or after blocks. If you are a chat assistant, put all blocks inside a single fenced ```text code block so they copy cleanly, or offer them as a downloadable .md file.

```text
=== DEAL <deal_id> ===
Date: <call.date>
Call type: <call.type>
Participants:
- <name> | <role> | <org> | internal
- <name> | <role> | <org> | external
---
[0] [00:00] <name> (<role>): <text>
[1] [00:12] <name> (<role>): <text>
...
--- EVIDENCE ---
{"budget": [12], "decision_timeline": [31, 44], "competitors": [7, 19]}
=== END <deal_id> ===
```

- One line per turn. Idx starts at 0 and has no gaps. The speaker name matches a participant exactly.
- `EVIDENCE` is a JSON object mapping each field whose status is not `not_mentioned` to the idx of every utterance that carries it (for `superseded`, include both the earlier and the final mention). Omit `not_mentioned` fields.
- Write the transcript first and the evidence last. Do not bend the dialogue to fit the evidence.
