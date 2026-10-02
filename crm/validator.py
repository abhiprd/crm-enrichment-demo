"""Deterministic hallucination check: every quote must appear in its cited utterance (SPEC section 3)."""

from __future__ import annotations

import re

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalize(text: str) -> str:
    """Lowercase, unify quote marks, drop punctuation, collapse whitespace, so formatting is not hallucination."""
    t = text.translate(_QUOTES).lower()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def quote_supported(quote: str, utterance: str) -> bool:
    q = normalize(quote)
    return bool(q) and q in normalize(utterance)


def validate_fields(fields: dict, utterances: dict) -> dict:
    """Return {field: reason} for each unsupported field. `utterances` maps idx -> text.

    A field with a value and status other than not_mentioned is unsupported if it has no evidence, cites an
    unknown idx, or any quote is not in its utterance. Unsupported fields never reach a proposal.
    """
    bad: dict = {}
    for f, spec in fields.items():
        if spec["status"] == "not_mentioned":
            continue
        ev = spec.get("evidence") or []
        if not ev:
            bad[f] = "no evidence"
            continue
        for e in ev:
            text = utterances.get(e["idx"])
            if text is None:
                bad[f] = f"unknown idx {e['idx']}"
            elif not quote_supported(e["quote"], text):
                bad[f] = f"quote not in utterance {e['idx']}"
            if f in bad:
                break
    return bad


def apply_validation(fields: dict, bad: dict) -> dict:
    """Unsupported fields are dropped (treated as not extracted) so they are scored as missing."""
    return {f: ({"value": None, "status": "not_mentioned", "evidence": []} if f in bad else spec)
            for f, spec in fields.items()}
