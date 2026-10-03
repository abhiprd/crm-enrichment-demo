You extract CRM facts from a sales call transcript. Read the whole call, then report what was said for each of the nine fields below. Use only the transcript. Do not guess facts that were not said.

## Participants

{{PARTICIPANTS}}

## Fields

For every field return `value`, `status`, and `evidence`.

- `budget`: amount in US dollars as a number (60000), or {"min": 80000, "max": 100000} for a range.
- `decision_timeline`: when the buyer will decide. An exact day as "YYYY-MM-DD", or a quarter as "YYYY-Qn", at the precision the speaker used.
- `competitors`: list of {"name": <vendor>, "stance": "evaluating" | "ruled_out" | "incumbent"}.
- `economic_buyer`: the full name of the person who controls the budget.
- `champion`: the full name of the person advocating for the purchase internally.
- `pain_points`: list from: {{PAIN_POINTS}}.
- `use_case`: list from: {{USE_CASES}}.
- `next_step`: {"action": one of {{NEXT_STEP_ACTIONS}}, "owner": <full name>, "date": "YYYY-MM-DD" or null}.
- `stage_signal`: one of advance, hold, regress, judged from the call as a whole.

`status` is one of:
- `stated`: said plainly.
- `hedged`: said, but without commitment ("probably", "if things go well").
- `negated`: explicitly ruled out.
- `superseded`: a value was said and then changed later in the call. Report the final value.
- `not_mentioned`: never discussed. Use `"value": null` and an empty `evidence` list.

`evidence` is a list of {"idx": <utterance number>, "quote": <exact words copied from that utterance>} for every utterance that supports the value. Quotes must be copied verbatim.

Calls take place on {{DATE}}. Resolve relative dates ("next Friday") to calendar dates.

## Output

Return only one JSON object, no prose and no code fences:

{"fields": {"budget": {...}, "decision_timeline": {...}, "competitors": {...}, "economic_buyer": {...}, "champion": {...}, "pain_points": {...}, "use_case": {...}, "next_step": {...}, "stage_signal": {...}}}

## Transcript

{{TRANSCRIPT}}
