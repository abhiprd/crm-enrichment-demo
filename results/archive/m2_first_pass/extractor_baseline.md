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

## How to judge each field

Use only the exact slugs listed for each field. `pain_points` and `use_case` have different lists: never put a use-case slug in `pain_points` or the reverse.

Be conservative. Report only what a speaker actually said in this call. Never infer a fact from what the rep demonstrates or offers, from a buyer politely reacting, or from a topic merely being related. When the call does not support a field, use `not_mentioned`: a wrong value is worse than a missing one.

- `status`: use `hedged` only when the speaker hedges explicitly ("probably", "if things go well", "we're hoping"). Use `negated` when the buyer explicitly drops or rules something out. If a value is stated and then changed later in the call, report the final value with status `superseded`; do not report the earlier one. Everything else said plainly is `stated`.
- `budget`: a figure the buyer gives for this purchase. A cost reaction without a figure is not a budget.
- `decision_timeline`: when the buyer says they will decide, not when they want to start or when a pilot ends.
- `competitors`: only a vendor toward which the call shows the buyer's own stance. `evaluating`: the buyer is comparing it. `ruled_out`: the buyer says they looked and dropped it or will not consider it. `incumbent`: the buyer uses it today. A vendor mentioned only as something another company uses has no stance for this buyer: leave it out. Never invent a stance value. If the buyer rules a vendor out, the field status is `negated` and the stance is `ruled_out`.
- `economic_buyer`: only when the call shows who approves, signs, or controls the money. Someone stating the budget, the timeline, or an opinion is not enough.
- `champion`: someone who clearly advocates for buying this internally (pushes for it, says they want it, will sell it to colleagues). Do not name a champion just because someone attends, asks questions, or is friendly, and a skeptic is never a champion. If nobody clearly pushes for the purchase, the champion is `not_mentioned`.
- `pain_points`: only problems the buyer states as their own, in their words. List one value per distinct problem, usually one or two. Do not add a value because it is plausible, because the rep raised it, or because it is a consequence of a problem already listed. If you are unsure whether to include a value, leave it out.
- `use_case`: only what the buyer says they want to use the product for. Features the rep shows do not count. If the buyer never says what they would use it for, it is `not_mentioned`. If you are unsure whether to include a value, leave it out.
- `next_step`: the final agreed next step, the person who committed to do it, and its date (null if no date was given). Pick the action that best matches: `send_security_docs` (security or compliance material), `send_proposal`, `send_contract`, `schedule_demo` (a demo of the product), `schedule_followup` (another call or meeting), `technical_review` (technical or integration review), `pilot_kickoff` (start of a pilot), `intro_to_buyer` (introduce the rep to the person who signs or to more stakeholders).

Calls take place on {{DATE}}. Resolve relative dates ("next Friday") to calendar dates.

## Output

Return only one JSON object, no prose and no code fences:

{"fields": {"budget": {...}, "decision_timeline": {...}, "competitors": {...}, "economic_buyer": {...}, "champion": {...}, "pain_points": {...}, "use_case": {...}, "next_step": {...}, "stage_signal": {...}}}

## Transcript

{{TRANSCRIPT}}
