"""SRE Incident Copilot: Streamlit UI.  Run: streamlit run app.py"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import streamlit as st

from src import feedback
from src.quick import collected_data, run_quick, ticket_markdown
from src.remediation import prepare_fix
from src.expert_engine import load_expertise
from src.output_validator import unapproved_dangerous_actions
from src.pipeline import Result, run_baseline, run_expert

ROOT = Path(__file__).parent
CASES = json.loads((ROOT / "evaluation" / "cases.json").read_text())
URGENCY_COLOR = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢", "unknown": "⚪"}
MODELS = ["gemma-4-26b-a4b-it", "gemma-4-31b-it"]

st.set_page_config(page_title="SRE Incident Copilot", page_icon="🧯", layout="wide")
st.title("🧯 SRE Incident Copilot")
st.caption("Storage-pressure alert triage for on-call engineers: deterministic risk math + explicit SRE expert rules + Gemma 4. "
           "Decision support only: it never executes anything.")

with st.sidebar:
    mode = st.radio("Mode", ["⚡ Quick assist", "🔬 Deep analysis"], index=1,
                    help="Quick assist: collect the data and draft the incident ticket + Slack update in ~10s. "
                         "Deep analysis: full expert plan, change correlation, history, comparison with plain Gemma, prepared fixes.")
    quick_mode = mode.startswith("⚡")
    st.header("Input")
    titles = ["✍️ Paste your own alert"] + [c["title"] for c in CASES]
    choice = st.selectbox("Demo scenario", titles, index=1)
    model = st.selectbox("Model (Gemini API)", MODELS)
    compare = st.toggle("Compare with plain Gemma (baseline)", value=True)
    use_changes = st.toggle("Use change feed (git, CI/CD, Jira, Slack)", value=True,
                            help="Synthetic feed in context/changes_feed.json, correlated with the alert by host / service / namespace.")
    use_history = st.toggle("Use team incident history (feedback loop)", value=True,
                            help="Similar past incidents and their outcomes are added to the prompt; actions that made things worse before always need approval.")
    use_cache = st.toggle("Replay recorded answers (offline / quota fallback)", value=False,
                          help="Unedited demo scenarios reuse the Gemma responses recorded by the evaluation run (recorded without incident history). Edited or new input always calls the API.")
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

tab_names = ["Analysis", "Expert rules", "Incident history", "Evaluation results"]
tab_main, tab_rules, tab_hist, tab_eval = st.tabs(tab_names)
OUTCOME_LABEL = {"helped": "✅ helped", "no_effect": "➖ no effect", "made_worse": "❌ made it worse"}


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


def render_fix(prep):
    with st.container(border=True):
        st.markdown(f"**🛠 Prepared fix:** {prep.action}")
        if prep.error:
            st.error(f"Could not prepare: {prep.error}")
            return
        r = prep.remediation
        if prep.status == "needs_human_decision":
            st.error("⛔ Needs a human decision. This will never be a one-click fix.")
        elif prep.allowlist:
            st.success(f"Allow-listed (`{prep.allowlist['id']}`, reversible: {prep.allowlist['reversible']}): "
                       "candidate for a future one-click fix (level 2).")
        else:
            st.info("Not allow-listed: prepared for manual review only.")
        for n in prep.guard_notes:
            st.warning(n)
        st.caption(f"{r.summary}  ·  confidence: {r.confidence}  ·  Nothing was executed. Review, then run yourself.")
        if r.preconditions:
            st.markdown("**Preconditions**\n" + "\n".join(f"- {p}" for p in r.preconditions))
        for title, items in (("1. Dry-run / preview", r.dry_run), ("2. Change", r.commands), ("4. Rollback", r.rollback)):
            if title.startswith("4") and r.verify:
                st.markdown("**3. Verify**")
                for c in r.verify:
                    st.code(c.command, language="bash")
                    if c.expected:
                        st.caption(f"expected: {c.expected}")
            if items:
                st.markdown(f"**{title}**")
                for c in items:
                    st.code(c.command, language="bash")
                    if c.explain:
                        st.caption(c.explain)
        if r.what_could_get_worse:
            st.markdown(f"**What could get worse:** {r.what_could_get_worse}")
        if r.blast_radius:
            st.markdown(f"**Blast radius:** {r.blast_radius}")
        if prep.allowlist:
            st.caption(f"Allow-list rollback hint: {prep.allowlist['rollback_hint']}")
        st.button("⚡ Apply fix (level 2: not available in the MVP)", disabled=True, key=f"apply-{id(prep)}",
                  help="One-click execution needs integrations, RBAC and an approval flow. See README → Vision.")


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

    if expert and res.changes:
        st.markdown("**Recent changes found by connectors**")
        for e in res.changes:
            st.markdown(f"- `{e.source}` **{e.id}** · {e.hours_ago:g}h ago · {e.author}: {e.title}  \n  *{e.reason}*")

    st.markdown("**Immediate actions**")
    for idx, a in enumerate(rep.immediate_actions):
        badge = "🔐 needs approval" if a.requires_approval else "✅ safe to do now"
        with st.expander(f"{a.action}  ·  {badge}"):
            if expert and st.button("🛠 Prepare fix", key=f"fix-btn-{idx}", help="Level 1: commands, dry-run, verification and rollback. Nothing is executed."):
                with st.spinner("Preparing commands, dry-run and rollback…"):
                    st.session_state.setdefault("fixes", {})[idx] = prepare_fix(
                        a, res.alert, res.risk, rep.applied_expert_rules, res.changes, res.meta.get("model"))
            for k in ("purpose", "conditions", "expected_effect", "risks", "verify"):
                if getattr(a, k):
                    st.markdown(f"- **{k.replace('_', ' ')}:** {getattr(a, k)}")
            if a.rule_ids:
                st.markdown(f"- **rules:** {', '.join(f'`{x}`' for x in a.rule_ids)}")
            if a.guard_note:
                st.warning(a.guard_note)
        if expert and idx in st.session_state.get("fixes", {}):
            render_fix(st.session_state["fixes"][idx])

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

    if expert and res.past_incidents:
        st.markdown("**Similar past incidents used**")
        for i in res.past_incidents:
            st.markdown(f"- {OUTCOME_LABEL[i.outcome]} `{i.id}` {i.ts}: {'; '.join(i.actions_taken)}  \n  *{i.note}*")

    if rep.applied_expert_rules:
        st.markdown("**Applied expert rules:** " + ", ".join(f"`{x}`" for x in rep.applied_expert_rules))
    if rep.incident_update:
        st.markdown("**Incident update (copy to Slack)**")
        st.code(rep.incident_update, language=None)
    st.caption(f"{res.meta.get('model')} · {res.meta.get('latency_s')}s" + (" · ♻️ replayed recorded answer" if res.meta.get("cached") else ""))


def render_quick(q):
    st.subheader("⚡ Quick assist: ticket drafted from collected data")
    c = st.columns(4)
    c[0].metric("Priority", q.priority)
    c[1].metric("Urgency (computed)", f"{URGENCY_COLOR.get(q.risk.urgency, '')} {q.risk.urgency}")
    c[2].metric("Time to exhaustion", f"{q.risk.time_to_exhaustion_hours} h" if q.risk.time_to_exhaustion_hours is not None else "unknown")
    c[3].metric("Changes found", len([e for e in q.changes if "global" not in e.targets]))
    with st.expander("Collected data (code, instant)"):
        st.json(collected_data(q))
    if q.error:
        st.error(f"Could not draft the ticket: {q.error}. The collected data above is still valid.")
        return
    md = ticket_markdown(q)
    with st.container(border=True):
        st.markdown(md)
    d1, d2 = st.columns(2)
    d1.download_button("⬇️ Download ticket (.md)", md, file_name=f"incident-{q.priority[:2]}.md", mime="text/markdown")
    with d2.popover("📋 Copy ticket markdown"):
        st.code(md, language="markdown")
    st.markdown("**Slack update**")
    st.code(q.draft.slack_update, language=None)
    st.caption(f"{q.meta.get('model')} · {q.meta.get('latency_s')}s · nothing was posted anywhere")
    with st.expander("Agent trace"):
        for t in q.trace:
            st.markdown(f"- **{t['step']}** ({t['ms']} ms): {t.get('detail', '')}")


with tab_main:
    if go and quick_mode:
        with st.spinner("Collecting data and drafting the ticket… (~10s)"):
            st.session_state["quick"] = run_quick(raw, model)
    if quick_mode and "quick" in st.session_state:
        render_quick(st.session_state["quick"])
    if go and not quick_mode:
        with st.spinner("Asking Gemma 4… (expert mode ~20-35s)"):
            with ThreadPoolExecutor(2) as pool:
                fe = pool.submit(run_expert, raw, model, use_cache, use_history and not use_cache, use_changes and not use_cache)
                fb = pool.submit(run_baseline, raw, model, use_cache) if compare else None
                exp_res = fe.result()
                base_res = fb.result() if fb else None
        st.session_state["last"] = (exp_res, base_res)
        st.session_state["fixes"] = {}
    if not quick_mode and "last" in st.session_state:
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
        if exp_res.report:
            st.subheader("🔁 Close the loop: what happened?")
            st.caption("Record the outcome once the incident is handled. It is saved to feedback/incidents.jsonl and used for similar alerts next time.")
            with st.form("feedback_form", clear_on_submit=True):
                outcome = st.radio("Outcome", list(OUTCOME_LABEL), format_func=OUTCOME_LABEL.get, horizontal=True)
                taken = st.multiselect("Actions you actually took", [a.action for a in exp_res.report.immediate_actions])
                other = st.text_input("Other action (optional)")
                note = st.text_area("What happened? (optional)", height=80)
                if st.form_submit_button("Save outcome"):
                    actions = taken + ([other] if other.strip() else [])
                    if not actions:
                        st.warning("Pick or describe at least one action.")
                    else:
                        inc = feedback.record(exp_res.alert, exp_res.report.assessment.urgency, actions, outcome, note,
                                              exp_res.report.applied_expert_rules)
                        st.success(f"Saved as {inc.id}. Next similar alert will see it.")
        with st.expander("Raw JSON"):
            st.json({"expert": exp_res.report.model_dump() if exp_res.report else exp_res.error,
                     "baseline": base_res.report.model_dump() if base_res and base_res.report else None})
    elif not (quick_mode and "quick" in st.session_state):
        st.info("Pick a scenario or paste an alert, then press **Analyze**.")

with tab_rules:
    st.markdown("Expertise lives in version-controlled YAML in [`expertise/`](expertise). The model gets only the rules that match the alert, and code re-checks the output against them.")
    exp = load_expertise()
    for f in sorted((ROOT / "expertise").glob("*.yaml")):
        with st.expander(f"{f.name}  ·  {sum(1 for r in exp.rules.values() if r.file == f.name)} rules"):
            st.code(f.read_text(), language="yaml")

with tab_hist:
    items = feedback.load_all()
    st.markdown(f"{len(items)} incidents · seed history is synthetic, new outcomes are saved in `feedback/incidents.jsonl`.")
    for i in sorted(items, key=lambda x: x.ts, reverse=True):
        st.markdown(f"- {OUTCOME_LABEL[i.outcome]} **{i.ts}** `{i.id}` {i.component}/{i.category} on {i.instance or '-'}"
                    f"{' (synthetic)' if i.synthetic else ''}: {'; '.join(i.actions_taken)}  \n  *{i.note}*")

with tab_eval:
    summary = ROOT / "evaluation" / "results" / "summary.md"
    st.markdown(summary.read_text() if summary.exists() else "Run `python -m evaluation.evaluate` to generate results.")
