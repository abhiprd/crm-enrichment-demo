"""Build the M1 hand-check worksheets: ~15 transcripts, stratified by split and case, seeded.

Each sheet puts the transcript beside the answer key (truth, status, and the cited utterances) so a human
can mark every field OK or ERROR. The sheets contain answer keys: under CLAUDE.md rule 1 the main Claude
session must not open results/handcheck/. Only the human reviewer does.

    python3 scripts/handcheck_sheet.py [--n 15] [--seed 7]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crm.schema import CASE_IDS, FIELDS  # noqa: E402

SPLIT_SHARE = {"learn": 0.4, "validation": 0.4, "test": 0.2}


def load(deal_id: str) -> tuple:
    key = json.loads((ROOT / "data" / "keys" / f"{deal_id}.json").read_text(encoding="utf-8"))
    text = (ROOT / "data" / "transcripts" / f"{deal_id}.md").read_text(encoding="utf-8")
    return key, text


def utterances(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.startswith("[") and "]" in line:
            idx = line[1:line.index("]")]
            if idx.isdigit():
                out[int(idx)] = line
    return out


def sample(keys: dict, n: int, seed: int) -> list:
    """Cover every case id (plus clean) first, then fill each split to its share."""
    rng = random.Random(seed)
    ids = sorted(keys)
    rng.shuffle(ids)
    quota = {s: round(n * share) for s, share in SPLIT_SHARE.items()}
    chosen: list = []
    for case in list(CASE_IDS) + ["clean"]:
        pool = [i for i in ids if i not in chosen and
                (case == "clean" and not keys[i]["cases"] or case in {c["id"] for c in keys[i]["cases"]})
                and quota[keys[i]["split"]] > 0]
        if pool and len(chosen) < n:
            pick = pool[0]
            chosen.append(pick)
            quota[keys[pick]["split"]] -= 1
    for i in ids:
        if len(chosen) >= n:
            break
        if i not in chosen and quota[keys[i]["split"]] > 0:
            chosen.append(i)
            quota[keys[i]["split"]] -= 1
    return sorted(chosen)


def render(deal_id: str, key: dict, text: str) -> str:
    utts = utterances(text)
    lines = [f"# Hand-check {deal_id} ({key['split']}) — cases: "
             f"{', '.join(c['id'] for c in key['cases']) or 'clean'}", ""]
    for c in key["cases"]:
        lines.append(f"- **Case `{c['id']}` on `{c['field']}`**: {c['note']}")
    lines += ["", "Mark each field OK or ERROR. ERROR means the call does not express the truth/status, "
              "hints at a `not_mentioned` field, renders a case note too plainly, or the cited idx is wrong.", "",
              "| Field | Truth | Status | Cited | Result | Note |", "| --- | --- | --- | --- | --- | --- |"]
    for f in FIELDS:
        spec = key["fields"][f]
        t = spec["truth"]
        lines.append(f"| {f} | `{json.dumps(t['value'])}` | {t['status']} | {spec['support_idx']} |  |  |")
    lines += ["", "## Cited utterances", ""]
    for f in FIELDS:
        for i in key["fields"][f]["support_idx"]:
            lines.append(f"- `{f}` [{i}]: {utts.get(i, '(missing)')[:300]}")
    lines += ["", "## Transcript", "", "```text", text.strip(), "```", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    keys = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "data" / "keys").glob("d*.json")}
    out = ROOT / "results" / "handcheck"
    out.mkdir(parents=True, exist_ok=True)
    picked = sample(keys, args.n, args.seed)
    for d in picked:
        key, text = load(d)
        (out / f"{d}.md").write_text(render(d, key, text), encoding="utf-8")
    index = {"seed": args.seed, "n": len(picked), "deals": picked,
             "result_template": {d: {"fields_checked": len(FIELDS), "errors": None} for d in picked}}
    (out / "sample.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"wrote {len(picked)} sheets to results/handcheck/ (do not open from the main Claude session)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
