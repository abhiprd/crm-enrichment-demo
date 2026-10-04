"""Extractor V0: transcript in, nine CRM-agnostic fields out. Never sees CRM values or house rules."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Settings
from .ingest import Block
from .llm import LLMResult, complete
from .schema import FIELDS, STATUSES

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extractor_v0.md"
TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "data" / "taxonomy.json"
DEFINITIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "taxonomy_definitions.json"


@dataclass
class Extraction:
    fields: dict = field(default_factory=dict)  # field -> {"value", "status", "evidence": [{"idx", "quote"}]}
    errors: list = field(default_factory=list)
    prompt_version: str = ""
    llm: Optional[LLMResult] = None
    retries: int = 0
    extra_cost: float = 0.0  # cost of a discarded first attempt


def load_template(path: Optional[Path] = None) -> str:
    return (Path(path) if path else PROMPT_PATH).read_text(encoding="utf-8")


def _plain(text: str) -> str:
    """Rule and example text is inserted into a template, so it must not look like a {{placeholder}}."""
    return text.replace("{{", "{ {").replace("}}", "} }")


def compose_template(template: str, rules: list, examples: Optional[dict] = None) -> str:
    """Insert 'Company conventions' (learned rules) and 'Reviewer corrections' (worked examples) before the
    transcript. With neither, the template is returned unchanged so its version hash does not move."""
    examples = {f: e for f, e in (examples or {}).items() if e}
    if not rules and not examples:
        return template
    parts = []
    if rules:
        lines = [f"- {r['field']}: {_plain(r['rule_text'])}" for r in rules]
        parts.append("## Company conventions\n\nThese rules come from reviewers of earlier calls. Follow them.\n\n"
                     + "\n".join(lines))
    if examples:
        lines = []
        for field, items in examples.items():
            for ex in items:
                quote = f' evidence "{_plain(ex["quote"])}":' if ex.get("quote") else ":"
                lines.append(f"- {field},{quote} a reviewer changed the proposed value `{_plain(ex['proposed'])}` "
                             f"to `{_plain(ex['final'])}`")
        parts.append("## Reviewer corrections\n\nEarlier proposals that a reviewer edited:\n\n" + "\n".join(lines))
    block = "\n\n".join(parts) + "\n\n"
    for marker in ("## Transcript", "{{TRANSCRIPT}}"):
        if marker in template:
            i = template.index(marker)
            return template[:i] + block + template[i:]
    return template.rstrip() + "\n\n" + block


def prompt_version(template: Optional[str] = None) -> str:
    """Short content hash of the prompt template, so a cached result is never reused after a prompt edit."""
    text = template if template is not None else load_template()
    return "pv-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def render_transcript(block: Block) -> str:
    return "\n".join(f"[{u.idx}] {u.speaker} ({u.role}): {u.text}" for u in block.utterances)


def build_prompt(block: Block, template: Optional[str] = None) -> str:
    tpl = template if template is not None else PROMPT_PATH.read_text(encoding="utf-8")
    tax = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    defs = json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))

    def described(kind: str) -> str:
        return "\n" + "\n".join(f"  - `{slug}`: {defs[kind][slug]}" for slug in tax[kind])
    people = "\n".join(f"- {p.name}, {p.role}, {p.org}" for p in block.participants)
    subs = {"{{PARTICIPANTS}}": people, "{{DATE}}": block.date, "{{TRANSCRIPT}}": render_transcript(block),
            "{{PAIN_POINTS}}": described("pain_points"), "{{USE_CASES}}": described("use_case"),
            "{{NEXT_STEP_ACTIONS}}": ", ".join(tax["next_step_action"])}
    for k, v in subs.items():
        tpl = tpl.replace(k, v)
    return tpl


def _loads(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(t[start:end + 1])


def parse_response(text: str) -> Extraction:
    """Stdlib schema check. A field that is missing or malformed becomes not_mentioned with an error logged."""
    ex = Extraction()
    try:
        raw = _loads(text).get("fields", {})
    except (json.JSONDecodeError, AttributeError) as e:
        ex.errors.append(f"unparseable JSON: {e}")
        raw = {}
    for f in FIELDS:
        spec = raw.get(f) if isinstance(raw, dict) else None
        if not isinstance(spec, dict) or spec.get("status") not in STATUSES:
            ex.errors.append(f"{f}: missing or invalid")
            ex.fields[f] = {"value": None, "status": "not_mentioned", "evidence": []}
            continue
        ev = [e for e in spec.get("evidence") or [] if isinstance(e, dict) and isinstance(e.get("idx"), int)
              and isinstance(e.get("quote"), str)]
        status = spec["status"]
        ex.fields[f] = {"value": None if status == "not_mentioned" else spec.get("value"), "status": status,
                        "evidence": ev}
    return ex


def extract(settings: Settings, conn: sqlite3.Connection, block: Block, *, model: str, effort: Optional[str],
            run_id: str, client=None, template: Optional[str] = None) -> Extraction:
    prompt = build_prompt(block, template)
    res = complete(settings, conn, prompt, model=model, effort=effort, run_id=run_id, purpose="extract",
                   client=client)
    ex = parse_response(res.text)
    retried, extra = 0, 0.0
    if any(e.startswith("unparseable") for e in ex.errors):  # sporadic non-JSON reply: ask once more
        extra = res.cost_usd
        res = complete(settings, conn, prompt, model=model, effort=effort, run_id=run_id, purpose="extract-retry",
                       client=client)
        ex, retried = parse_response(res.text), 1
    ex.prompt_version, ex.llm, ex.retries, ex.extra_cost = prompt_version(template), res, retried, extra
    return ex
