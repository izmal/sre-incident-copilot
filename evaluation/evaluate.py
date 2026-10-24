"""Baseline (Gemma alone) vs Expert mode (Gemma + SRE Incident Copilot) on the same cases.

Usage:  python -m evaluation.evaluate [--no-cache] [--model gemma-4-31b-it]
Scoring is automatic and reproducible (regex rubric in cases.json, see rubric.md).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.expert_engine import load_expertise  # noqa: E402
from src.output_validator import unapproved_dangerous_actions  # noqa: E402
from src.pipeline import run_baseline, run_expert, to_dict  # noqa: E402
from src.schemas import URGENCY_ORDER, Report  # noqa: E402

CASES = ROOT / "evaluation" / "cases.json"
OUT = ROOT / "evaluation" / "results"
CRITERIA = ["urgency", "risks", "safety", "diagnostics", "unknowns"]


def _txt(*parts) -> str:
    return " ".join(json.dumps(p) if not isinstance(p, str) else p for p in parts).lower()


def _groups_score(groups: list[list[str]], text: str) -> tuple[int, list[str]]:
    if not groups:
        return 2, []
    missed = [g[0] for g in groups if not any(re.search(p, text, re.I) for p in g)]
    frac = 1 - len(missed) / len(groups)
    return (2 if frac == 1 else 1 if frac >= 0.5 else 0), missed


def score(report: Report | None, expected: dict) -> dict:
    if report is None:
        return {"total": 0, "critical_errors": [], "schema_ok": False, **{c: 0 for c in CRITERIA}}
    r = report
    got = (r.assessment.urgency or "unknown").lower().strip()
    exp = expected["urgency"]
    if got in exp:
        u = 2
    elif got in URGENCY_ORDER and any(e in URGENCY_ORDER and abs(URGENCY_ORDER.index(e) - URGENCY_ORDER.index(got)) == 1 for e in exp):
        u = 1
    else:
        u = 0
    everything = _txt(r.model_dump())
    diag_text = _txt([s.model_dump() for s in r.investigation_steps], r.root_cause_hypotheses,
                     [a.model_dump() for a in r.immediate_actions])
    unk_text = _txt([m.model_dump() for m in r.missing_context])
    risks, risks_missed = _groups_score(expected.get("risks", []), everything)
    diag, diag_missed = _groups_score(expected.get("diagnostics", []), diag_text)
    unk, unk_missed = _groups_score(expected.get("unknowns", []), unk_text)
    crit = unapproved_dangerous_actions(r, load_expertise())
    safety = 0 if crit else 2
    return {"urgency": u, "urgency_got": got, "risks": risks, "safety": safety, "diagnostics": diag, "unknowns": unk,
            "total": u + risks + safety + diag + unk, "critical_errors": crit, "schema_ok": True,
            "missed": {"risks": risks_missed, "diagnostics": diag_missed, "unknowns": unk_missed}}


def run_case(case: dict, model: str | None, use_cache: bool) -> dict:
    b = run_baseline(case["input"], model=model, use_cache=use_cache)
    e = run_expert(case["input"], model=model, use_cache=use_cache)
    return {
        "id": case["id"], "title": case["title"],
        "baseline": {"score": score(b.report, case["expected"]), "latency_s": b.meta.get("latency_s"), "result": to_dict(b)},
        "expert_llm_only": {"score": score(e.raw_report, case["expected"])},
        "expert": {"score": score(e.report, case["expected"]), "latency_s": e.meta.get("latency_s"),
                   "guard_log": e.guard_log, "result": to_dict(e)},
    }


def summarize(rows: list[dict], model: str) -> str:
    modes = [("baseline", "Baseline (Gemma alone)"), ("expert_llm_only", "Expert, before code guard"), ("expert", "Expert (final)")]
    n = len(rows)
    lines = [f"# Evaluation: baseline vs expert mode", "",
             f"Model: `{model}` · cases: {n} · generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
             "scoring: automatic regex rubric (`evaluation/rubric.md`)", "",
             "## Totals", "", "| Mode | Avg score /10 | Critical safety errors | Correct urgency | Schema OK |", "|---|---|---|---|---|"]
    for key, label in modes:
        sc = [r[key]["score"] for r in rows]
        avg = sum(s["total"] for s in sc) / n
        crit = sum(1 for s in sc if s["critical_errors"])
        urg = sum(1 for s in sc if s["urgency"] == 2)
        ok = sum(1 for s in sc if s["schema_ok"])
        lines.append(f"| {label} | **{avg:.1f}** | {crit} | {urg}/{n} | {ok}/{n} |")
    lines += ["", "## Per criterion (sum over cases, max " + str(2 * n) + ")", "",
              "| Criterion | Baseline | Expert before guard | Expert final |", "|---|---|---|---|"]
    for c in CRITERIA:
        vals = [sum(r[k]["score"].get(c, 0) for r in rows) for k, _ in modes]
        lines.append(f"| {c} | {vals[0]} | {vals[1]} | {vals[2]} |")
    lines += ["", "## Per case", "", "| Case | Expected urgency | Baseline | Expert | Baseline urgency | Expert urgency | Guard interventions |",
              "|---|---|---|---|---|---|---|"]
    cases = {c["id"]: c for c in json.loads(CASES.read_text())}
    for r in rows:
        b, e = r["baseline"]["score"], r["expert"]["score"]
        flag_b = " ⚠️" if b["critical_errors"] else ""
        lines.append(f"| {r['title']} | {'/'.join(cases[r['id']]['expected']['urgency'])} | {b['total']}{flag_b} | {e['total']} | "
                     f"{b.get('urgency_got', '-')} | {e.get('urgency_got', '-')} | {len(r['expert']['guard_log'])} |")
    crit_rows = [(r["title"], k, ce) for r in rows for k, _ in modes for ce in r[k]["score"]["critical_errors"]]
    if crit_rows:
        lines += ["", "## Critical safety errors (dangerous action proposed without human approval)", ""]
        lines += [f"- **{t}** [{k}]: {ce['label']} (`{ce['rule_id']}`): \"{ce['action'][:140]}\"" for t, k, ce in crit_rows]
    lat_b = [r["baseline"]["latency_s"] for r in rows if r["baseline"]["latency_s"]]
    lat_e = [r["expert"]["latency_s"] for r in rows if r["expert"]["latency_s"]]
    if lat_b and lat_e:
        lines += ["", f"Latency (avg, uncached calls): baseline {sum(lat_b)/len(lat_b):.1f}s, expert {sum(lat_e)/len(lat_e):.1f}s."]
    lines += ["", "⚠️ = critical safety error. Small synthetic dataset: results are indicative, not proof of general superiority."]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--only", default=None, help="comma-separated case ids")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    from src.llm_provider import DEFAULT_MODEL
    model = args.model or DEFAULT_MODEL
    cases = json.loads(CASES.read_text())
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(lambda c: run_case(c, model, not args.no_cache), cases))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(rows, indent=2, default=str))
    md = summarize(rows, model)
    (OUT / "summary.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
