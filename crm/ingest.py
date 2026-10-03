"""Ingest generated transcripts from inbox/ into the dataset.

An inbox file holds one or more blocks, from a Claude chat or the transcript-writer agent:

    === DEAL d001 ===
    Date: 2026-09-14
    Call type: discovery
    Participants:
    - Dana Reyes | Account Executive | Northbeam | internal
    - Priya Shah | VP Finance | Kestrel Foods | external
    ---
    [0] [00:00] Dana Reyes (Account Executive): Thanks for making time.
    ...
    --- EVIDENCE ---
    {"budget": [12], "decision_timeline": [31]}
    === END d001 ===

Each valid block becomes data/transcripts/<id>.md (call metadata and utterances only) and,
for eval deals, data/keys/<id>.json (truth copied from deals.json plus the cited idx).
Deal ids starting with "demo-" are live-demo calls: evidence is optional and no key is written.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .deals import DealSet, load_deals
from .paths import Paths
from .schema import CALL_TYPES, DEMO_PREFIX, FIELDS, LEAK_TOKENS, MIN_TURNS

DEAL_RE = re.compile(r"^===\s*DEAL\s+(\S+)\s*===$")
END_RE = re.compile(r"^===\s*END\s+(\S+)\s*===$")
EVIDENCE_RE = re.compile(r"^---\s*EVIDENCE\s*---$")
FENCE_RE = re.compile(r"^```")
UTT_RE = re.compile(r"^\[(\d+)\]\s+\[(\d{1,3}):(\d{2})\]\s+(.+?)\s+\(([^()]+)\):\s+(.+)$")
INBOX_SUFFIXES = {".md", ".txt"}


@dataclass
class Participant:
    name: str
    role: str
    org: str
    internal: bool


@dataclass
class Utterance:
    idx: int
    seconds: int
    speaker: str
    role: str
    text: str


@dataclass
class Block:
    deal_id: str
    date: str = ""
    call_type: str = ""
    participants: list[Participant] = field(default_factory=list)
    utterances: list[Utterance] = field(default_factory=list)
    evidence_text: str = ""
    parse_errors: list[str] = field(default_factory=list)

    @property
    def is_demo(self) -> bool:
        return self.deal_id.startswith(DEMO_PREFIX)


@dataclass
class BlockResult:
    deal_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    wrote: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class FileResult:
    path: Path
    blocks: list[BlockResult] = field(default_factory=list)
    file_errors: list[str] = field(default_factory=list)
    file_warnings: list[str] = field(default_factory=list)
    moved_to: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.file_errors and all(b.ok for b in self.blocks)


# ---------------------------------------------------------------- parsing

def parse_file(text: str) -> tuple[list[Block], list[str], list[str]]:
    """Split inbox text into blocks. Returns (blocks, file_errors, file_warnings)."""
    blocks: list[Block] = []
    errors: list[str] = []
    stray = 0
    current: list[str] | None = None
    current_id = ""
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if FENCE_RE.match(line.strip()):
            continue  # chat replies often wrap output in code fences
        m_start, m_end = DEAL_RE.match(line.strip()), END_RE.match(line.strip())
        if m_start:
            if current is not None:
                errors.append(f"line {lineno}: DEAL {m_start.group(1)} opened before END {current_id}")
            current, current_id = [], m_start.group(1)
        elif m_end:
            if current is None:
                errors.append(f"line {lineno}: END {m_end.group(1)} with no open DEAL")
            elif m_end.group(1) != current_id:
                errors.append(f"line {lineno}: END {m_end.group(1)} does not match DEAL {current_id}")
                current = None
            else:
                blocks.append(parse_block(current_id, current))
                current = None
        elif current is not None:
            current.append(line)
        elif line.strip():
            stray += 1
    if current is not None:
        errors.append(f"DEAL {current_id} never closed with END {current_id}")
    if not blocks and not errors:
        errors.append("no DEAL blocks found")
    warnings = [f"ignored {stray} line(s) outside DEAL blocks"] if stray else []
    return blocks, errors, warnings


def parse_block(deal_id: str, lines: list[str]) -> Block:
    b = Block(deal_id=deal_id)
    section = "header"
    in_participants = False
    evidence: list[str] = []
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if section == "header":
            if s == "---":
                section = "body"
            elif s.lower().startswith("date:"):
                b.date, in_participants = s.split(":", 1)[1].strip(), False
            elif s.lower().startswith("call type:"):
                b.call_type, in_participants = s.split(":", 1)[1].strip().lower(), False
            elif s.lower().startswith("participants:"):
                in_participants = True
            elif in_participants and s.startswith("-"):
                parts = [p.strip() for p in s[1:].split("|")]
                if len(parts) != 4 or parts[3].lower() not in ("internal", "external"):
                    b.parse_errors.append(f"header line {i}: participant must be 'name | role | org | internal|external'")
                else:
                    b.participants.append(Participant(parts[0], parts[1], parts[2], parts[3].lower() == "internal"))
            elif s:
                b.parse_errors.append(f"header line {i}: unexpected '{s[:60]}'")
        elif section == "body":
            if EVIDENCE_RE.match(s):
                section = "evidence"
            elif s:
                m = UTT_RE.match(s)
                if not m:
                    b.parse_errors.append(f"body line {i}: not '[idx] [mm:ss] Speaker (Role): text': '{s[:60]}'")
                else:
                    idx, mm, ss, spk, role, text = m.groups()
                    b.utterances.append(Utterance(int(idx), int(mm) * 60 + int(ss), spk.strip(), role.strip(), text.strip()))
        else:
            evidence.append(line)
    if section == "header":
        b.parse_errors.append("missing '---' line between header and utterances")
    b.evidence_text = "\n".join(evidence).strip()
    return b


def parse_transcript(text: str) -> Block:
    """Parse a stored data/transcripts/<id>.md file (same format, no markers or evidence)."""
    return parse_block("transcript", text.splitlines())


# ------------------------------------------------------------- validation

def leak_hits(text: str) -> list[str]:
    hits = []
    for tok in LEAK_TOKENS:
        hay, needle = (text, tok) if tok.isupper() else (text.lower(), tok.lower())
        if needle in hay:
            hits.append(tok)
    return hits


def validate_block(b: Block, dealset: DealSet) -> tuple[list[str], list[str], dict]:
    """Return (errors, warnings, evidence_by_field)."""
    errs = list(b.parse_errors)
    warns: list[str] = []
    deal = dealset.deals.get(b.deal_id)
    if not b.is_demo and deal is None:
        errs.append(f"deal {b.deal_id} is not in data/deals.json (live-demo ids must start with '{DEMO_PREFIX}')")

    # header
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", b.date or ""):
        errs.append(f"Date must be YYYY-MM-DD, got '{b.date}'")
    if b.call_type not in CALL_TYPES:
        errs.append(f"Call type must be one of {CALL_TYPES}, got '{b.call_type}'")
    if len(b.participants) < 2 or not any(p.internal for p in b.participants) \
            or all(p.internal for p in b.participants):
        errs.append("need at least one internal and one external participant")
    if deal:
        if b.date and b.date != deal["call"]["date"]:
            errs.append(f"Date {b.date} does not match deals.json ({deal['call']['date']})")
        if b.call_type and b.call_type != deal["call"]["type"]:
            errs.append(f"Call type {b.call_type} does not match deals.json ({deal['call']['type']})")
        want = {p["name"] for p in deal["participants"]}
        got = {p.name for p in b.participants}
        if want != got:
            errs.append(f"participants differ from deals.json: missing {sorted(want - got)}, extra {sorted(got - want)}")

    # utterances
    names = {p.name for p in b.participants}
    if len(b.utterances) < MIN_TURNS:
        errs.append(f"only {len(b.utterances)} turns; need at least {MIN_TURNS}")
    for pos, u in enumerate(b.utterances):
        if u.idx != pos:
            errs.append(f"utterance idx must run 0..n-1 without gaps; position {pos} has [{u.idx}]")
            break
    for prev, u in zip(b.utterances, b.utterances[1:]):
        if u.seconds < prev.seconds:
            errs.append(f"timestamp goes backwards at [{u.idx}]")
            break
    unknown = sorted({u.speaker for u in b.utterances} - names)
    if unknown:
        errs.append(f"speakers not in Participants: {unknown}")
    target = deal["call"].get("target_turns") if deal else None
    if target and len(b.utterances) < 0.6 * target:
        warns.append(f"{len(b.utterances)} turns vs target {target}")
    leaks = sorted({t for u in b.utterances for t in leak_hits(u.text)})
    if leaks:
        errs.append(f"transcript text leaks labels: {leaks}")

    # evidence
    evidence: dict = {}
    if b.evidence_text:
        try:
            evidence = json.loads(b.evidence_text)
            if not isinstance(evidence, dict):
                raise ValueError("not an object")
        except (json.JSONDecodeError, ValueError) as e:
            errs.append(f"EVIDENCE is not a JSON object: {e}")
            evidence = {}
    elif not b.is_demo:
        errs.append("EVIDENCE section missing")
    unknown_fields = sorted(set(evidence) - set(FIELDS))
    if unknown_fields:
        errs.append(f"EVIDENCE has unknown fields: {unknown_fields}")
    max_idx = len(b.utterances) - 1
    for f, idxs in evidence.items():
        if f not in FIELDS:
            continue
        if not isinstance(idxs, list) or not all(isinstance(i, int) for i in idxs):
            errs.append(f"EVIDENCE.{f} must be a list of utterance idx integers")
            continue
        bad = [i for i in idxs if i < 0 or i > max_idx]
        if bad:
            errs.append(f"EVIDENCE.{f} cites idx outside 0..{max_idx}: {bad}")
    if deal:
        for f in FIELDS:
            status = deal["fields"][f]["truth"]["status"]
            cited = evidence.get(f) or []
            if status == "not_mentioned" and cited:
                errs.append(f"{f} is not_mentioned in deals.json but EVIDENCE cites {cited}")
            if status != "not_mentioned" and not cited:
                errs.append(f"{f} is {status} in deals.json but EVIDENCE cites nothing")
    return errs, warns, evidence


# ---------------------------------------------------------------- writing

def render_transcript(b: Block) -> str:
    lines = [f"Date: {b.date}", f"Call type: {b.call_type}", "Participants:"]
    for p in b.participants:
        lines.append(f"- {p.name} | {p.role} | {p.org} | {'internal' if p.internal else 'external'}")
    lines.append("---")
    for u in b.utterances:
        lines.append(f"[{u.idx}] [{u.seconds // 60:02d}:{u.seconds % 60:02d}] {u.speaker} ({u.role}): {u.text}")
    return "\n".join(lines) + "\n"


def build_key(deal: dict, evidence: dict, source: Path, digest: str) -> dict:
    fields = {}
    for f in FIELDS:
        spec = deal["fields"][f]
        fields[f] = {
            "truth": spec["truth"],
            "expected_proposal": spec.get("expected_proposal"),
            "crm_relation": spec["crm_relation"],
            "crm_before": spec.get("crm_before"),
            "support_idx": evidence.get(f) or [],
        }
    return {
        "deal_id": deal["deal_id"],
        "split": deal["split"],
        "cases": deal.get("cases", []),
        "call": deal["call"],
        "fields": fields,
        "source": {
            "file": source.name,
            "sha256": digest,
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def ingest_file(path: Path, paths: Paths, dealset: DealSet, force: bool = False) -> FileResult:
    text = path.read_text(encoding="utf-8")
    result = FileResult(path=path)
    blocks, result.file_errors, result.file_warnings = parse_file(text)
    seen: set[str] = set()
    for b in blocks:
        br = BlockResult(deal_id=b.deal_id)
        result.blocks.append(br)
        if b.deal_id in seen:
            br.errors.append("deal appears twice in this file")
            continue
        seen.add(b.deal_id)
        br.errors, br.warnings, evidence = validate_block(b, dealset)
        t_path = paths.transcripts / f"{b.deal_id}.md"
        if t_path.exists() and not force:
            br.errors.append(f"{t_path.relative_to(paths.root)} already exists (use --force to replace)")
        if br.errors:
            continue
        transcript = render_transcript(b)
        _atomic_write(t_path, transcript)
        br.wrote.append(str(t_path.relative_to(paths.root)))
        if not b.is_demo:
            digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
            key = build_key(dealset.deals[b.deal_id], evidence, path, digest)
            k_path = paths.keys / f"{b.deal_id}.json"
            _atomic_write(k_path, json.dumps(key, indent=2) + "\n")
            br.wrote.append(str(k_path.relative_to(paths.root)))
            stale_audit = paths.audit / f"{b.deal_id}.json"
            if stale_audit.exists():
                stale_audit.unlink()  # a new transcript needs a new verification
    return result


def _move(path: Path, dest_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{stamp}_{path.name}"
    shutil.move(str(path), dest)
    return dest


def pending_files(paths: Paths, settle_seconds: float = 1.0) -> list[Path]:
    now = time.time()
    return sorted(
        p for p in paths.inbox.iterdir()
        if p.is_file() and p.suffix in INBOX_SUFFIXES and not p.name.startswith(".")
        and now - p.stat().st_mtime >= settle_seconds
    )


def run_pipeline(deal_id: str, paths: Paths, live: bool = False) -> str:
    """Hand an ingested demo transcript to the live pipeline. Eval transcripts are never sent."""
    if not deal_id.startswith(DEMO_PREFIX):
        return "not a demo call: ingested only (eval transcripts never go to the live pipeline)"
    from . import pipeline
    out = pipeline.process_interaction(deal_id, paths, live=live)
    if not out.get("matched"):
        return out["message"]
    return (f"{'posted card' if live else 'dry-run'}: {out['proposals']} proposal(s) for {out['deal']}"
            + (f", unsupported {out['unsupported_fields']}" if out["unsupported_fields"] else ""))


def ingest_inbox(paths: Paths, force: bool = False, run: bool = False,
                 files: list[Path] | None = None, settle_seconds: float = 1.0,
                 live: bool = False) -> list[FileResult]:
    paths.ensure()
    dealset = load_deals(paths)
    results = []
    for f in files if files is not None else pending_files(paths, settle_seconds):
        r = ingest_file(f, paths, dealset, force=force)
        if f.parent.resolve() == paths.inbox.resolve():
            r.moved_to = _move(f, paths.processed if r.ok else paths.rejected)
            if not r.ok:
                r.moved_to.with_suffix(r.moved_to.suffix + ".errors.txt").write_text(format_result(r), encoding="utf-8")
        if run:
            for b in r.blocks:
                if b.ok:
                    b.warnings.append(run_pipeline(b.deal_id, paths, live))
        results.append(r)
    return results


def format_result(r: FileResult) -> str:
    out = [f"{r.path.name}: {'OK' if r.ok else 'REJECTED'}"]
    out += [f"  file error: {e}" for e in r.file_errors]
    out += [f"  file warning: {w}" for w in r.file_warnings]
    for b in r.blocks:
        out.append(f"  {b.deal_id}: {'ok' if b.ok else 'FAILED'}")
        out += [f"    error: {e}" for e in b.errors]
        out += [f"    warning: {w}" for w in b.warnings]
        out += [f"    wrote: {w}" for w in b.wrote]
    if r.moved_to:
        out.append(f"  moved to {r.moved_to.parent.name}/{r.moved_to.name}")
    return "\n".join(out)


def watch(paths: Paths, interval: float, force: bool, run: bool, live: bool = False) -> None:
    print(f"watching {paths.inbox} every {interval}s (Ctrl+C to stop)", flush=True)
    try:
        while True:
            for r in ingest_inbox(paths, force=force, run=run, live=live):
                print(format_result(r), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
