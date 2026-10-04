You maintain the instructions for an extractor that reads sales call transcripts and proposes CRM updates. A reviewer rejected one proposal. Decide whether the rejection reveals a general mistake the extractor should avoid on future calls, and if so write ONE short rule.

## The rejected proposal

- Field: {{FIELD}}
- What the extractor reported (status {{STATUS}}): {{VALUE}}
- What the CRM held before: {{CURRENT}}
- Reviewer's reason code: {{REASON}} ({{REASON_MEANING}})
- Reviewer's note: {{NOTE}}
- Evidence the extractor cited:
{{QUOTES}}

## Rules already in force for this field

{{EXISTING}}

## What to write

- One rule, one field (`{{FIELD}}`), as an instruction to the extractor: imperative, plain English, at most 280 characters.
- General: it must hold for any company. Never mention a person, company, product, or number from this call.
- It must not tell the extractor to invent or assume anything, and must not repeat an existing rule.
- If the rejection looks like a one-off, a display problem, or a reviewer preference that could not be applied consistently, do not write a rule.

Return only one JSON object, no prose and no code fences:

{"rule_text": "<the rule>", "rationale": "<one sentence: the mistake it prevents>"}

or, if no general rule applies:

{"rule_text": null, "rationale": "<one sentence: why no rule>"}
