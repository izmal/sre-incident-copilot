"""SRE Incident Copilot: Streamlit UI.  Run: streamlit run app.py"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import streamlit as st

from src.expert_engine import load_expertise
from src.output_validator import unapproved_dangerous_actions
from src.pipeline import Result, prepare_expert, run_baseline, run_expert

ROOT = Path(__file__).parent
CASES = json.loads((ROOT / "evaluation" / "cases.json").read_text())
URGENCY_COLOR = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢", "unknown": "⚪"}
MODELS = ["gemma-4-26b-a4b-it", "gemma-4-31b-it"]

st.set_page_config(page_title="SRE Incident Copilot", page_icon="🧯", layout="wide")
st.title("🧯 SRE Incident Copilot")
st.caption("Storage-pressure alert triage for on-call engineers: deterministic risk math + explicit SRE expert rules + Gemma 4. "
           "Decision support only: it never executes anything.")

with st.sidebar:
    st.header("Input")
    titles = ["✍️ Paste your own alert"] + [c["title"] for c in CASES]
    choice = st.selectbox("Demo scenario", titles, index=1)
    model = st.selectbox("Model (Gemini API)", MODELS)
    compare = st.toggle("Compare with plain Gemma (baseline)", value=True)
    use_cache = st.toggle("Replay recorded answers (offline / quota fallback)", value=False,
                          help="Unedited demo scenarios reuse the Gemma responses recorded by the evaluation run. Edited or new input always calls the API.")
    st.markdown("---")
    st.markdown("Accepts **Alertmanager webhook JSON**, a single alert, `{alerts, observed_metrics, context}` or plain text.")
    st.markdown("Use synthetic or anonymized data only: the text is sent to the Gemini API.")

if choice.startswith("✍️"):
    default = ""
else:
    case = next(c for c in CASES if c["title"] == choice)
    default = case["input"] if isinstance(case["input"], str) else json.dumps(case["input"], indent=2)
raw = st.text_area("Alert payload / description", value=default, height=260, key=f"input-{choice}")
go = st.button("Analyze", type="primary", disabled=not raw.strip())

tab_names = ["Analysis", "Expert rules", "Evaluation results"]
tab_main, tab_rules, tab_eval = st.tabs(tab_names)


def render_risk(res: Result):
    r = res.risk
    c = st.columns(5)
    c[0].metric("Urgency (computed)", f"{URGENCY_COLOR.get(r.urgency, '')} {r.urgency}")
    c[1].metric("Time to exhaustion", f"{r.time_to_exhaustion_hours} h" if r.time_to_exhaustion_hours is not None else "unknown")
    c[2].metric("Used", f"{r.used_pct}%" if r.used_pct is not None else "unknown")
    c[3].metric("Growth", f"{r.growth_gib_per_hour} GiB/h" if r.growth_gib_per_hour is not None else "unknown")
    c[4].metric("Evidence", f"{int(r.evidence_completeness * 100)}%")
    st.caption(f"{' '.join(r.urgency_reasons)} {r.tte_note}  · Monitoring severity label: `{res.alert.severity_label}` (kept separate)")
    for w in r.warnings:
        st.warning(w)


def render_report(res: Result, expert: bool):
    if res.error:
        st.error(f"LLM unavailable or invalid output: {res.error}")
        if expert and res.risk:
            st.info("Showing the deterministic analysis only. No AI conclusion was generated. Retry in a moment.")
        return
    rep = res.report
    u = rep.assessment.urgency
    st.markdown(f"#### {URGENCY_COLOR.get(u, '⚪')} Urgency: **{u}**")
    st.markdown(rep.incident.summary)
    if rep.assessment.rationale:
        st.caption(rep.assessment.rationale)

    if not expert:
        crit = unapproved_dangerous_actions(rep, load_expertise())
        for ce in crit:
            st.error(f"⚠️ Unsafe: '{ce['action']}' is a {ce['label']} proposed without human approval ({ce['rule_id']})")

    st.markdown("**Immediate actions**")
    for a in rep.immediate_actions:
        badge = "🔐 needs approval" if a.requires_approval else "✅ safe to do now"
        with st.expander(f"{a.action}  ·  {badge}"):
            for k in ("purpose", "conditions", "expected_effect", "risks", "verify"):
                if getattr(a, k):
                    st.markdown(f"- **{k.replace('_', ' ')}:** {getattr(a, k)}")
            if a.rule_ids:
                st.markdown(f"- **rules:** {', '.join(f'`{x}`' for x in a.rule_ids)}")
            if a.guard_note:
                st.warning(a.guard_note)

    st.markdown("**Missing context (what to check next)**")
    for m in rep.missing_context:
        st.markdown(f"- **{m.item}**" + (f": {m.why}" if m.why else "") + (f"  \n  `{m.how_to_check}`" if m.how_to_check else ""))

    st.markdown("**Root-cause investigation**")
    for s in rep.investigation_steps:
        st.markdown(f"- {s.step}" + (f" *({s.hypothesis})*" if s.hypothesis else "") + (f"  \n  `{s.how_to_check}`" if s.how_to_check else ""))

    if rep.dangerous_operations:
        st.markdown("**Do NOT do without approval**")
        for d in rep.dangerous_operations:
            st.markdown(f"- ⛔ {d.operation}: {d.why_dangerous}" + (f" `{d.rule_id}`" if d.rule_id else ""))

    if rep.applied_expert_rules:
        st.markdown("**Applied expert rules:** " + ", ".join(f"`{x}`" for x in rep.applied_expert_rules))
    if rep.incident_update:
        st.markdown("**Incident update (copy to Slack)**")
        st.code(rep.incident_update, language=None)
    st.caption(f"{res.meta.get('model')} · {res.meta.get('latency_s')}s" + (" · ♻️ replayed recorded answer" if res.meta.get("cached") else ""))


with tab_main:
    if go:
        alert, risk, rules, _ = prepare_expert(raw)
        with st.spinner("Asking Gemma 4… (expert mode ~20-35s)"):
            with ThreadPoolExecutor(2) as pool:
                fe = pool.submit(run_expert, raw, model, use_cache)
                fb = pool.submit(run_baseline, raw, model, use_cache) if compare else None
                exp_res = fe.result()
                base_res = fb.result() if fb else None
        st.session_state["last"] = (exp_res, base_res)
    if "last" in st.session_state:
        exp_res, base_res = st.session_state["last"]
        st.subheader("Deterministic risk analysis (code, not LLM)")
        render_risk(exp_res)
        cols = st.columns(2) if base_res else [st.container()]
        with cols[0]:
            st.subheader("🧯 SRE Incident Copilot (expert mode)")
            render_report(exp_res, expert=True)
        if base_res:
            with cols[1]:
                st.subheader("🤖 Plain Gemma 4 (baseline)")
                render_report(base_res, expert=False)
        with st.expander("Agent trace: pipeline steps"):
            for t in exp_res.trace:
                st.markdown(f"- **{t['step']}** ({t['ms']} ms): {t.get('detail', '')}")
            if exp_res.guard_log:
                st.markdown("**Safety guard interventions:**")
                for g in exp_res.guard_log:
                    st.markdown(f"- {g}")
        with st.expander("Raw JSON"):
            st.json({"expert": exp_res.report.model_dump() if exp_res.report else exp_res.error,
                     "baseline": base_res.report.model_dump() if base_res and base_res.report else None})
    else:
        st.info("Pick a scenario or paste an alert, then press **Analyze**.")

with tab_rules:
    st.markdown("Expertise lives in version-controlled YAML in [`expertise/`](expertise). The model gets only the rules that match the alert, and code re-checks the output against them.")
    exp = load_expertise()
    for f in sorted((ROOT / "expertise").glob("*.yaml")):
        with st.expander(f"{f.name}  ·  {sum(1 for r in exp.rules.values() if r.file == f.name)} rules"):
            st.code(f.read_text(), language="yaml")

with tab_eval:
    summary = ROOT / "evaluation" / "results" / "summary.md"
    st.markdown(summary.read_text() if summary.exists() else "Run `python -m evaluation.evaluate` to generate results.")
