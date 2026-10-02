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


@dataclass
class Extraction:
    fields: dict = field(default_factory=dict)  # field -> {"value", "status", "evidence": [{"idx", "quote"}]}
    errors: list = field(default_factory=list)
    prompt_version: str = ""
    llm: Optional[LLMResult] = None


def prompt_version(template: Optional[str] = None) -> str:
    text = template if template is not None else PROMPT_PATH.read_text(encoding="utf-8")
    return "v0-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def render_transcript(block: Block) -> str:
    return "\n".join(f"[{u.idx}] {u.speaker} ({u.role}): {u.text}" for u in block.utterances)


def build_prompt(block: Block, template: Optional[str] = None) -> str:
    tpl = template if template is not None else PROMPT_PATH.read_text(encoding="utf-8")
    tax = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    people = "\n".join(f"- {p.name}, {p.role}, {p.org}" for p in block.participants)
    subs = {"{{PARTICIPANTS}}": people, "{{DATE}}": block.date, "{{TRANSCRIPT}}": render_transcript(block),
            "{{PAIN_POINTS}}": ", ".join(tax["pain_points"]), "{{USE_CASES}}": ", ".join(tax["use_case"]),
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
            run_id: str, client=None) -> Extraction:
    prompt = build_prompt(block)
    res = complete(settings, conn, prompt, model=model, effort=effort, run_id=run_id, purpose="extract",
                   client=client)
    ex = parse_response(res.text)
    ex.prompt_version, ex.llm = prompt_version(), res
    return ex
