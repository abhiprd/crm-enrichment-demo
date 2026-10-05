"""M6: static SVG charts and the unit-economics numbers, generated from files under results/ (and the SQLite
spend log) with the standard library only. A chart never holds a number that is not in a results file.

Charts are plain SVG that follow the reader's light/dark setting. Categorical slots 1-3 of the reference palette
(blue, orange, aqua), in fixed order; every chart has a legend or direct labels, and README carries the numbers."""

from __future__ import annotations

import html
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

from . import stats
from .paths import Paths

W, H = 720, 400
STYLE = """<style>
:root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e3e2de;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#8a8984}
@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#34332f;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#8a8984}}
text{font-family:system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif;fill:var(--ink)}
.t{font-size:16px;font-weight:600}.sub{font-size:12px;fill:var(--ink2)}.ax{font-size:11px;fill:var(--ink2)}
.lab{font-size:12px}.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--ink2);stroke-width:1}
</style>"""
SERIES = ("var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)")


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def head(title: str, subtitle: str, w: int = W, h: int = H) -> list:
    return [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="{esc(title)}. {esc(subtitle)}">', STYLE, f'<rect width="{w}" height="{h}" fill="var(--bg)"/>',
            f'<text class="t" x="24" y="28">{esc(title)}</text>', f'<text class="sub" x="24" y="46">{esc(subtitle)}</text>']


class Plot:
    """A linear plot area: maps data to pixels and draws the recessive grid and axes."""

    def __init__(self, x0: float, x1: float, y0: float, y1: float, left: int = 60, right: int = 130, top: int = 108,
                 bottom: int = 70, w: int = W, h: int = H):
        self.x0, self.x1, self.y0, self.y1 = x0, x1, y0, y1
        self.l, self.r, self.t, self.b, self.w, self.h = left, w - right, top, h - bottom, w, h

    def px(self, x: float) -> float:
        return self.l + (x - self.x0) / (self.x1 - self.x0) * (self.r - self.l)

    def py(self, y: float) -> float:
        return self.b - (y - self.y0) / (self.y1 - self.y0) * (self.b - self.t)

    def frame(self, yticks: list, xticks: list, ylabel: str, xlabel: str, fmt=lambda v: f"{v:.1f}") -> list:
        out = []
        for v in yticks:
            out.append(f'<line class="grid" x1="{self.l}" x2="{self.r}" y1="{self.py(v):.1f}" y2="{self.py(v):.1f}"/>')
            out.append(f'<text class="ax" x="{self.l - 8}" y="{self.py(v) + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
        out.append(f'<line class="axis" x1="{self.l}" x2="{self.r}" y1="{self.b}" y2="{self.b}"/>')
        for v in xticks:
            out.append(f'<text class="ax" x="{self.px(v):.1f}" y="{self.b + 16}" text-anchor="middle">{esc(v)}</text>')
        out.append(f'<text class="ax" x="{(self.l + self.r) / 2:.0f}" y="{self.b + 34}" text-anchor="middle">{esc(xlabel)}</text>')
        out.append(f'<text class="ax" transform="translate(16 {(self.t + self.b) / 2:.0f}) rotate(-90)" text-anchor="middle">{esc(ylabel)}</text>')
        return out


def legend(items: list, x: int = 24, y: int = 68, width: int = W - 48) -> list:
    """items: (label, color, dashed). Swatch lines in a row that wraps before it would leave the chart."""
    out, cx, cy = [], x, y
    for label, color, dashed in items:
        need = 28 + 7 * len(label) + 22
        if cx + need - 22 > x + width and cx > x:
            cx, cy = x, cy + 16
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        out.append(f'<line x1="{cx}" x2="{cx + 22}" y1="{cy}" y2="{cy}" stroke="{color}" stroke-width="2.5"{dash}/>')
        out.append(f'<text class="lab" x="{cx + 28}" y="{cy + 4}">{esc(label)}</text>')
        cx += need
    return out


def spread(ys: list, gap: float = 14.0) -> list:
    """Nudge label y positions apart so direct labels never overlap (order preserved)."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    out = list(ys)
    for a, b in zip(order, order[1:]):
        if out[b] - out[a] < gap:
            out[b] = out[a] + gap
    return out


def polyline(pl: Plot, pts: list, color: str, dashed: bool = False, markers: bool = True) -> list:
    d = " ".join(f"{pl.px(x):.1f},{pl.py(y):.1f}" for x, y in pts)
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out = [f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="2"{dash}/>']
    if markers:
        out += [f'<circle cx="{pl.px(x):.1f}" cy="{pl.py(y):.1f}" r="3.5" fill="{color}" stroke="var(--bg)" stroke-width="2"/>' for x, y in pts]
    return out


def _load(paths: Paths, name: str) -> dict:
    return json.loads((paths.root / "results" / name).read_text(encoding="utf-8"))


# ---- individual charts ------------------------------------------------------------------------------

def chart_curve(curve: dict, prop: dict) -> str:
    """Validation score by learning batch, all fields and by instance group, with V0 and the baseline as lines."""
    pts = curve["points"]
    pl = Plot(0, len(pts) - 1, 0.0, 1.0)
    allp = [(p["batch"], p["mean_score"]) for p in pts]
    house = [(p["batch"], p["by_group"]["house_rule"]["mean"]) for p in pts]
    trap = [(p["batch"], p["by_group"]["trap"]["mean"]) for p in pts]
    v0, bl = prop["v0"]["mean_score"], prop["manual_baseline"]["mean_score"]
    out = head("Validation score by learning batch (proposal level)",
               f"{pts[0]['repeats']} runs per point, {pts[0]['calls'] // pts[0]['repeats']} validation transcripts; oracle reviewer, rules gated")
    out += legend([("All fields", SERIES[0], False), ("House-rule instances", SERIES[1], False), ("Trap instances", SERIES[2], False),
                   (f"V0, 5-run mean ({v0:.3f})", SERIES[0], True), (f"Manual baseline, 5-run mean ({bl:.3f})", SERIES[3], True)])
    out += pl.frame([0, 0.25, 0.5, 0.75, 1.0], [p["batch"] for p in pts], "Mean proposal-level score",
                    "Learning batch (5 learn transcripts each; 0 = empty ruleset)", lambda v: f"{v:.2f}")
    for ref, col in ((v0, SERIES[0]), (bl, SERIES[3])):
        out.append(f'<line x1="{pl.l}" x2="{pl.r}" y1="{pl.py(ref):.1f}" y2="{pl.py(ref):.1f}" stroke="{col}" stroke-width="1.5" stroke-dasharray="5 4" opacity="0.8"/>')
    ends = [allp[-1][1], house[-1][1], trap[-1][1]]
    ys = spread([pl.py(v) for v in ends])
    for (series, col), y, v in zip(((allp, SERIES[0]), (house, SERIES[1]), (trap, SERIES[2])), ys, ends):
        out += polyline(pl, series, col)
        out.append(f'<text class="lab" x="{pl.r + 10}" y="{y + 4:.1f}">{v:.3f}</text>')
    out.append(f'<text class="ax" x="{pl.l - 8}" y="{pl.b + 52}" text-anchor="end">rules</text>')
    out.append(f'<text class="ax" x="{pl.l - 8}" y="{pl.b + 64}" text-anchor="end">examples</text>')
    for p in pts:
        out.append(f'<text class="ax" x="{pl.px(p["batch"]):.1f}" y="{pl.b + 52}" text-anchor="middle">{len(p["active_rule_ids"])}</text>')
        out.append(f'<text class="ax" x="{pl.px(p["batch"]):.1f}" y="{pl.b + 64}" text-anchor="middle">{p["examples"]}</text>')
    return "\n".join(out + ["</svg>"])


def chart_test(test: dict) -> str:
    """Test split: proposal-level and truth-level mean score per arm, side by side (same 0-1 scale)."""
    arms = [("v0", "V0"), ("manual_baseline", "Manual baseline"), ("final", "Final (rules + examples)")]
    pl = Plot(0, 3, 0.0, 1.0, right=40)
    n = test["transcripts"]
    out = head("Test split: proposal-level vs truth-level score",
               f"{n} test transcripts x 9 fields, {test['repeats']} runs per arm; the final arm was tuned toward the proposal metric")
    out += legend([("Proposal level (expected CRM proposal)", SERIES[0], False), ("Truth level (what was said)", SERIES[1], False)])
    out += pl.frame([0, 0.25, 0.5, 0.75, 1.0], [], "Mean score", "", lambda v: f"{v:.2f}")
    bw = 52
    for i, (key, label) in enumerate(arms):
        cx = pl.px(i + 0.5)
        a = test["arms"][key]
        for j, (val, col) in enumerate(((a["mean_score"], SERIES[0]), (a["truth_mean_score"], SERIES[1]))):
            x = cx - bw - 2 + j * (bw + 4)
            y = pl.py(val)
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw}" height="{pl.b - y:.1f}" rx="3" fill="{col}"/>')
            out.append(f'<text class="lab" x="{x + bw / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle">{val:.3f}</text>')
        out.append(f'<text class="lab" x="{cx:.1f}" y="{pl.b + 18}" text-anchor="middle">{esc(label)}</text>')
    return "\n".join(out + ["</svg>"])


def chart_ablation(ab: dict) -> str:
    """Validation ablation: the five run scores per arm (dots) and their mean (bar), on one axis."""
    rows = [("V0 (empty ruleset)", ab["v0"]["run_scores"])] + [(n.replace("_", " ").replace("fewshot", "few-shot"), a["run_scores"])
                                                              for n, a in ab["arms"].items()]
    lo, hi = 0.76, 0.94
    pl = Plot(lo, hi, 0, len(rows), left=170, right=60, top=84, bottom=64, h=330)
    out = head("Ablation on validation: final state with and without each ingredient", f"{len(rows[0][1])} runs per arm, 40 validation transcripts; dot = one run, tick = mean",
               h=330)
    out += [f'<line class="grid" x1="{pl.px(v):.1f}" x2="{pl.px(v):.1f}" y1="{pl.t}" y2="{pl.b}"/>' for v in (0.8, 0.85, 0.9)]
    out += [f'<text class="ax" x="{pl.px(v):.1f}" y="{pl.b + 16}" text-anchor="middle">{v:.2f}</text>' for v in (0.8, 0.85, 0.9)]
    out.append(f'<text class="ax" x="{(pl.l + pl.r) / 2:.0f}" y="{pl.b + 34}" text-anchor="middle">Mean proposal-level score per run (axis starts at 0.76)</text>')
    for i, (name, runs) in enumerate(rows):
        y = pl.t + (i + 0.5) * (pl.b - pl.t) / len(rows)
        col = SERIES[3] if i == 0 else SERIES[0]
        out.append(f'<text class="lab" x="{pl.l - 10}" y="{y + 4:.1f}" text-anchor="end">{esc(name)}</text>')
        out.append(f'<line x1="{pl.px(min(runs)):.1f}" x2="{pl.px(max(runs)):.1f}" y1="{y:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="2" opacity="0.5"/>')
        out += [f'<circle cx="{pl.px(r):.1f}" cy="{y:.1f}" r="4" fill="{col}" stroke="var(--bg)" stroke-width="2"/>' for r in runs]
        m = sum(runs) / len(runs)
        out.append(f'<line x1="{pl.px(m):.1f}" x2="{pl.px(m):.1f}" y1="{y - 12:.1f}" y2="{y + 12:.1f}" stroke="var(--ink)" stroke-width="2"/>')
        out.append(f'<text class="lab" x="{pl.r + 8}" y="{y + 4:.1f}">{m:.3f}</text>')
    return "\n".join(out + ["</svg>"])


def review_error_rates(curve: dict) -> list:
    """Per batch: (batch, edited+rejected, reviewed, rate, lo, hi) with a Wilson interval."""
    out = []
    for r in curve["reviews"]:
        k = r["edited"] + r["rejected"]
        rate, lo, hi = stats.wilson(k, r["reviewed"])
        out.append((r["batch"], k, r["reviewed"], rate, lo, hi))
    return out


def chart_review_error(curve: dict) -> str:
    rows = review_error_rates(curve)
    pl = Plot(1, len(rows), 0.0, 0.6, right=40, h=380)
    out = head("Share of proposals the oracle edited or rejected, by batch",
               "Each batch is reviewed with the version in force at that point; bands are 95% Wilson intervals", h=380)
    out += pl.frame([0, 0.2, 0.4, 0.6], [r[0] for r in rows], "Edited or rejected / reviewed", "Learning batch", lambda v: f"{v:.1f}")
    up = " ".join(f"{pl.px(r[0]):.1f},{pl.py(r[5]):.1f}" for r in rows)
    dn = " ".join(f"{pl.px(r[0]):.1f},{pl.py(r[4]):.1f}" for r in reversed(rows))
    out.append(f'<polygon points="{up} {dn}" fill="{SERIES[0]}" opacity="0.15"/>')
    out += polyline(pl, [(r[0], r[3]) for r in rows], SERIES[0])
    for r in rows:
        out.append(f'<text class="ax" x="{pl.px(r[0]):.1f}" y="{pl.py(r[3]) - 10:.1f}" text-anchor="middle">{r[1]}/{r[2]}</text>')
    return "\n".join(out + ["</svg>"])


def reason_mix(curve: dict) -> tuple:
    """(reasons in fixed order, per-batch counts). The top three reasons overall get their own slot; the rest are 'other'."""
    total = Counter()
    for r in curve["reviews"]:
        total.update(r["reject_reasons"])
    top = [k for k, _ in total.most_common(3)]
    rows = []
    for r in curve["reviews"]:
        c = {k: r["reject_reasons"].get(k, 0) for k in top}
        c["other"] = sum(v for k, v in r["reject_reasons"].items() if k not in top)
        rows.append((r["batch"], c))
    return top + ["other"], rows


def chart_reasons(curve: dict) -> str:
    reasons, rows = reason_mix(curve)
    top = max(1, max(sum(c.values()) for _, c in rows))
    pl = Plot(0.5, len(rows) + 0.5, 0, top + 1, right=40, h=380)
    out = head("Reject reasons by batch (oracle reviewer)", "Counts of rejected proposals; reasons come from the reviewer's reason code", h=380)
    out += legend([(r.replace("_", " "), SERIES[i], False) for i, r in enumerate(reasons)])
    out += pl.frame(list(range(0, top + 2, 2)), [r[0] for r in rows], "Rejected proposals", "Learning batch", lambda v: f"{v:.0f}")
    bw = 34
    for b, c in rows:
        y = pl.b
        for i, reason in enumerate(reasons):
            h = (pl.b - pl.py(c[reason])) if c[reason] else 0
            if h:
                out.append(f'<rect x="{pl.px(b) - bw / 2:.1f}" y="{y - h + 1:.1f}" width="{bw}" height="{h - 2:.1f}" fill="{SERIES[i]}"/>')
                y -= h
    return "\n".join(out + ["</svg>"])


def rule_inventory(curve: dict) -> list:
    ev = curve["rule_events"]
    made = [e for e in ev if e["status"] == "created"]
    return [("Candidate rules drafted", len(made)), ("Passed the gate, now active", sum(bool(e["gate_passed"]) for e in made)),
            ("Failed the gate, not activated", sum(not e["gate_passed"] for e in made)),
            ("Learner declined to write a rule", sum(e["status"] == "skipped" for e in ev))]


def chart_rules(curve: dict) -> str:
    rows = rule_inventory(curve)
    top = max(v for _, v in rows)
    pl = Plot(0, top + 1, 0, len(rows), left=230, right=60, top=70, bottom=40, h=250)
    out = head("Rule inventory after the learning curve",
               "Rejects the oracle made, at most 3 candidates per batch; each candidate is gated on validation", h=250)
    for i, (label, v) in enumerate(rows):
        y = pl.t + i * (pl.b - pl.t) / len(rows) + 6
        bh = (pl.b - pl.t) / len(rows) - 12
        out.append(f'<text class="lab" x="{pl.l - 10}" y="{y + bh / 2 + 4:.1f}" text-anchor="end">{esc(label)}</text>')
        out.append(f'<rect x="{pl.l}" y="{y:.1f}" width="{max(pl.px(v) - pl.l, 0):.1f}" height="{bh:.1f}" rx="3" fill="{SERIES[0]}"/>')
        out.append(f'<text class="lab" x="{pl.px(v) + 8:.1f}" y="{y + bh / 2 + 4:.1f}">{v}</text>')
    return "\n".join(out + ["</svg>"])


# ---- unit economics ---------------------------------------------------------------------------------

def _pct(xs: list, q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def call_stats(conn: sqlite3.Connection, model: str, purpose: str, effort: Optional[str]) -> dict:
    rows = conn.execute("SELECT cost_usd, latency_ms, input_tokens, output_tokens FROM llm_calls WHERE model=? AND purpose=? "
                        "AND COALESCE(effort,'')=?", (model, purpose, effort or "")).fetchall()
    n = len(rows)
    mean = lambda i: (sum(r[i] for r in rows) / n) if n else 0.0  # noqa: E731
    return {"calls": n, "mean_cost_usd": mean(0), "mean_latency_ms": mean(1), "p50_latency_ms": _pct([r[1] for r in rows], 0.5),
            "p95_latency_ms": _pct([r[1] for r in rows], 0.95), "mean_input_tokens": mean(2), "mean_output_tokens": mean(3),
            "total_cost_usd": sum(r[0] for r in rows)}


def fill_rate(paths: Paths) -> dict:
    """CRM fill rate over the hand-reviewed calls: of the 8 CRM-written fields per call (stage signal is a note),
    how many held a value before the call, and how many after the reviewer's approved or edited proposals.
    Reads crm_before from the keys in code; writes counts only."""
    conn = sqlite3.connect(str(paths.root / "results" / "handreview.sqlite"))
    conn.row_factory = sqlite3.Row
    deals = sorted({r["deal_id"] for r in conn.execute("SELECT deal_id FROM proposals")})
    fields = ("budget", "decision_timeline", "competitors", "economic_buyer", "champion", "pain_points", "use_case", "next_step")
    before = after = 0
    for d in deals:
        key = json.loads((paths.keys / f"{d}.json").read_text(encoding="utf-8"))["fields"]
        applied = {r["field"] for r in conn.execute("SELECT field FROM proposals WHERE deal_id=? AND review_status IN ('approved','edited') "
                                                    "AND action!='clear'", (d,))}
        for f in fields:
            filled = bool(key[f].get("crm_before"))
            before += filled
            after += filled or f in applied
    cells = len(deals) * len(fields)
    return {"calls": len(deals), "fields_per_call": len(fields), "cells": cells, "filled_before": before, "filled_after": after,
            "fill_rate_before": before / cells if cells else None, "fill_rate_after": after / cells if cells else None,
            "note": "after = filled before, or an approved/edited proposal on that field, from the hand-reviewed decisions (one reviewer)"}


def unit_economics(paths: Paths, extractor_model: str, learner_model: str, effort: Optional[str]) -> dict:
    main = sqlite3.connect(str(paths.root / "results" / "crm.sqlite"))
    curve = sqlite3.connect(str(paths.root / "results" / "curve.sqlite"))
    learner = [call_stats(c, learner_model, "rule-learner", "low") for c in (main, curve)]
    n = sum(x["calls"] for x in learner)
    return {"source": "local spend logs (results/crm.sqlite, results/curve.sqlite, results/handreview.sqlite; git-ignored, so this file regenerates only locally). Extractor stats: requests with purpose extract at the selected model and effort, pooled across every run at that setting (validation, test, noise floor, baseline, gates, ablation, demo; includes archived first-pass runs); excludes extract-retry, debug and the effort-low and effort-medium bake-off calls. Learner stats: purpose rule-learner at effort low, both logs.",
            "extractor": {"model": extractor_model, "effort": effort or "default", **call_stats(main, extractor_model, "extract", effort)},
            "rule_learner": {"model": learner_model, "calls": n,
                             "mean_cost_usd": sum(x["total_cost_usd"] for x in learner) / n if n else 0.0,
                             "mean_latency_ms": sum(x["mean_latency_ms"] * x["calls"] for x in learner) / n if n else 0.0},
            "crm_fill_rate": fill_rate(paths)}


def build_all(paths: Paths, extractor_model: str, learner_model: str, effort: Optional[str]) -> list:
    """Write docs/charts/*.svg and results/unit_economics.json. Returns the files written."""
    curve, prop = _load(paths, "curve.json"), _load(paths, "proposal_scores.json")
    charts = {"curve.svg": chart_curve(curve, prop), "test.svg": chart_test(_load(paths, "test_run.json")),
              "ablation.svg": chart_ablation(_load(paths, "ablation.json")), "review_error.svg": chart_review_error(curve),
              "reject_reasons.svg": chart_reasons(curve), "rules.svg": chart_rules(curve)}
    out_dir = paths.root / "docs" / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, svg in charts.items():
        (out_dir / name).write_text(svg + "\n", encoding="utf-8")
        written.append(f"docs/charts/{name}")
    econ = unit_economics(paths, extractor_model, learner_model, effort)
    (paths.root / "results" / "unit_economics.json").write_text(json.dumps(econ, indent=2), encoding="utf-8")
    return written + ["results/unit_economics.json"]
