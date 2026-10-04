"""SQLite log: interactions, extractions, proposals, rules, and spend. Append-only where SPEC says so."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
  id TEXT PRIMARY KEY, source_type TEXT NOT NULL, occurred_at TEXT, participants TEXT,
  deal_ref TEXT, source_meta TEXT
);
CREATE TABLE IF NOT EXISTS utterances (
  id INTEGER PRIMARY KEY AUTOINCREMENT, interaction_id TEXT NOT NULL REFERENCES interactions(id),
  idx INTEGER NOT NULL, speaker TEXT, is_internal INTEGER, start_ts TEXT, text TEXT NOT NULL,
  UNIQUE (interaction_id, idx)
);
CREATE TABLE IF NOT EXISTS ruleset_versions (
  version_id INTEGER PRIMARY KEY AUTOINCREMENT, parent_version_id INTEGER, active_rule_ids TEXT NOT NULL DEFAULT '[]',
  prompt_template_hash TEXT, change_type TEXT, changed_rule_id INTEGER, reason TEXT, author TEXT,
  validation_score_before REAL, validation_score_after REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS rules (
  rule_id INTEGER PRIMARY KEY AUTOINCREMENT, field TEXT NOT NULL, rule_text TEXT NOT NULL, rationale TEXT,
  source_proposal_ids TEXT NOT NULL DEFAULT '[]', created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  status TEXT NOT NULL CHECK (status IN ('candidate','active','retired','reverted')),
  validated INTEGER, gate_result TEXT
);
CREATE TABLE IF NOT EXISTS extractions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, interaction_id TEXT NOT NULL REFERENCES interactions(id),
  field TEXT NOT NULL, value TEXT,
  status TEXT NOT NULL CHECK (status IN ('stated','hedged','negated','superseded','not_mentioned')),
  confidence REAL, evidence TEXT, prompt_version TEXT, model TEXT, ruleset_version_id INTEGER
);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, extraction_id INTEGER, deal_id TEXT, hubspot_object TEXT NOT NULL,
  hubspot_id TEXT, property TEXT NOT NULL, current_value TEXT, proposed_value TEXT,
  action TEXT NOT NULL CHECK (action IN ('set','append','clear','note')), tentative INTEGER NOT NULL DEFAULT 0,
  evidence TEXT, context TEXT, interaction_id TEXT, field TEXT, note TEXT,
  review_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (review_status IN ('pending','approved','edited','rejected','no_response','bulk_approved')),
  final_value TEXT, reject_reason TEXT, reviewed_by TEXT, reviewed_at TEXT, slack_ts TEXT, slack_channel TEXT,
  ruleset_version_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT DEFAULT CURRENT_TIMESTAMP, notes TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, model TEXT NOT NULL, purpose TEXT,
  input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, effort TEXT,
  cost_usd REAL, latency_ms INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_calls)")}
    for col, typ in (("reasoning_tokens", "INTEGER"), ("effort", "TEXT")):  # older logs predate these
        if col not in cols:
            conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} {typ}")
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(rules)")}
    for col, typ in (("validated", "INTEGER"), ("gate_result", "TEXT")):
        if col not in rcols:
            conn.execute(f"ALTER TABLE rules ADD COLUMN {col} {typ}")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(proposals)")}
    for col in ("context", "interaction_id", "field", "note"):  # older logs predate these columns
        if col not in pcols:
            conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} TEXT")
    return conn
