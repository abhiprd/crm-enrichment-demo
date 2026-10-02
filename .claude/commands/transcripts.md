---
description: Generate the next batch of transcripts with the transcript-writer agent
argument-hint: "[--n 5] [--split learn]"
---
1. Check `ANTHROPIC_API_KEY` is not set in the shell (`[ -z "$ANTHROPIC_API_KEY" ]`). If it is, stop and tell me to unset it.
2. Run `python3 -m crm request $ARGUMENTS` (default `--n 5`). Note the request file path it prints.
3. Launch the `transcript-writer` agent with that request file path. It writes the reply into `inbox/`.
4. Report which deal ids were written. Do not open the written transcripts' answer keys. Then suggest `/ingest`.
