---
description: Ingest inbox transcripts, then verify them with the transcript-verifier agent
---
1. Run `python3 -m crm ingest`. Report ingested, failed blocks, and rejected files (read `inbox/rejected/*.errors.txt`).
2. Run `python3 -m crm status` and find transcripts with no verdict in `data/audit/`.
3. Launch the `transcript-verifier` agent on those deal ids (batches of up to 5). It writes `data/audit/<id>.json`.
4. Report pass/fail counts with denominators. For failures, list the deal ids and suggest re-requesting them with `python3 -m crm request --deals <ids>` and `/transcripts`.
Fix rejects before generating more, so prompt problems surface early.
