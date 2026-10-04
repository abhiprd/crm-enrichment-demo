"""The learning step that follows a reject: draft a rule, put it in force (or hold it), validate it on the
validation split, and keep or retire it. `notify` receives one short message per step (a Slack thread reply)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from . import db, gate, learner, rules
from .config import Settings
from .paths import Paths


def learn_and_apply(settings: Settings, paths: Paths, pid: int, notify: Callable[[str], None],
                    client=None, gate_client=None, validate: bool = True) -> dict:
    """reject -> candidate rule -> (demo: active, unvalidated | gated: candidate) -> gate -> keep or retire.

    Demo mode (`rules_mode == "demo"`) puts the rule in force at once, flagged unvalidated, so the next call is
    affected; gated mode waits for the gate. `validate=False` skips the gate (no keys or noise file available)."""
    conn = db.connect(paths.root / "results" / "crm.sqlite")
    res = learner.learn_from_reject(settings, conn, pid, client=client)
    if res.status != "created":
        notify(f"No new rule from this rejection: {res.reason}")
        return {"status": "no_rule", "reason": res.reason}
    rule = rules.get_rule(conn, res.rule_id)
    out = {"status": "created", "rule_id": res.rule_id, "field": rule["field"], "rule_text": res.rule_text}
    if settings.rules_mode == "demo":
        version, why = rules.activate(conn, res.rule_id, rule["created_by"] or "reviewer",
                                      "demo mode: in force before validation", validated=False)
        if version is None:
            notify(f"New rule for {rule['field']} stays a candidate: {why}")
            return {**out, "status": "candidate", "reason": why}
        out["version"] = version
        notify(f"New rule for {rule['field']} (unvalidated, ruleset v{version}): {res.rule_text}")
    else:
        notify(f"New candidate rule for {rule['field']}, not in force until validated: {res.rule_text}")
    if not validate:
        return out
    try:
        result = gate.validate_rule(settings, conn, paths, res.rule_id, client=gate_client)
    except (SystemExit, OSError, RuntimeError) as e:  # missing noise file, keys, or transcripts
        notify(f"Validation could not run ({e}); the rule stays {'unvalidated' if 'version' in out else 'a candidate'}.")
        return {**out, "gate": "unavailable"}
    summary = gate.apply_verdict(conn, res.rule_id, result)
    notify(f"Rule {res.rule_id} {summary}")
    return {**out, "gate": result["passed"], "summary": summary, "net": result["net_by_field"][rule["field"]]}
