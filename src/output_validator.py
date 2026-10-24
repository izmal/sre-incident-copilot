"""Code-level guardrails on the LLM output. Safety does not depend on the prompt alone."""
from __future__ import annotations

import re

from .expert_engine import Expertise
from .feedback import matches_bad_outcome
from .schemas import Action, DangerousOp, Report, RiskAnalysis


def action_text(a: Action) -> str:
    return " ".join([a.action, a.purpose, a.conditions, a.expected_effect])


NEGATION = re.compile(r"\b(do not|don't|never|avoid|stop|halt|pause|refrain from|instead of|without)\b[^.;]*$", re.I)


def find_danger(text: str, expertise: Expertise) -> list[tuple[str, str]]:
    """A match counts unless it is negated in the same sentence ("Do NOT run deleteMany")."""
    hits = []
    for rid, label, pat in expertise.danger_patterns():
        for m in pat.finditer(text):
            if not NEGATION.search(text[max(0, m.start() - 60):m.start()]):
                hits.append((rid, label))
                break
    return hits


def unapproved_dangerous_actions(report: Report, expertise: Expertise) -> list[dict]:
    """Dangerous operations proposed as immediate actions WITHOUT human approval = critical safety error."""
    hits = []
    for a in report.immediate_actions:
        if a.requires_approval:
            continue
        for rid, label in find_danger(action_text(a), expertise):
            hits.append({"action": a.action, "rule_id": rid, "label": label})
    return hits


def validate_and_guard(report: Report, risk: RiskAnalysis, expertise: Expertise,
                       allowed_rule_ids: set[str], past_incidents: list | None = None) -> tuple[Report, list[str]]:
    """Returns the guarded report and a log of every intervention (shown in the UI trace)."""
    log: list[str] = []

    # 1. Rule ids must exist and be among the rules we actually gave the model.
    real = [r for r in report.applied_expert_rules if r in allowed_rule_ids]
    fake = sorted(set(report.applied_expert_rules) - set(real))
    if fake:
        log.append(f"Removed unknown rule ids: {', '.join(fake)}")
    report.applied_expert_rules = list(dict.fromkeys(real))
    for a in report.immediate_actions:
        a.rule_ids = [r for r in a.rule_ids if r in allowed_rule_ids]

    # 2. Urgency is owned by the deterministic analyzer.
    llm_urgency = report.assessment.urgency
    if llm_urgency != risk.urgency:
        log.append(f"Urgency set to deterministic value '{risk.urgency}' (model said '{llm_urgency}').")
        report.assessment.rationale = (f"[computed] {' '.join(risk.urgency_reasons)} "
                                       f"[model] {report.assessment.rationale}").strip()
        report.assessment.urgency = risk.urgency

    # 3. Dangerous operations can never be unapproved immediate actions.
    known_ops = {d.operation.lower() for d in report.dangerous_operations}
    for a in report.immediate_actions:
        hits = find_danger(action_text(a), expertise)
        if not hits:
            continue
        rids = sorted({h[0] for h in hits})
        labels = sorted({h[1] for h in hits})
        if not a.requires_approval:
            a.requires_approval = True
            a.guard_note = f"Guard: forced human approval ({', '.join(labels)}; {', '.join(rids)})."
            log.append(f"Forced approval on '{a.action[:80]}' ({', '.join(labels)}).")
        for rid in rids:
            if rid not in report.applied_expert_rules:
                report.applied_expert_rules.append(rid)
        for label in labels:
            if label not in known_ops:
                report.dangerous_operations.append(DangerousOp(operation=label, why_dangerous=expertise.rules[rids[0]].title, rule_id=rids[0]))
                known_ops.add(label)

    # 4. An action that made things worse in a similar past incident always needs approval.
    for a in report.immediate_actions:
        bad = matches_bad_outcome(a.action, past_incidents or [])
        if bad:
            note = f"Past incident {bad.id} ({bad.ts}): a similar action MADE IT WORSE. {bad.note}"
            if not a.requires_approval:
                a.requires_approval = True
                log.append(f"Forced approval on '{a.action[:80]}': made things worse in {bad.id}.")
            a.guard_note = (a.guard_note + " " + note).strip()

    # 5. Missing context from the analyzer must be visible even if the model forgot it.
    listed = " ".join(m.item.lower() for m in report.missing_context)
    for k in risk.missing_context:
        words = k.replace("_bytes", "").replace("_", " ")
        if words.split()[0] not in listed:
            from .schemas import MissingItem
            report.missing_context.append(MissingItem(item=words, why="Required by an applied expert rule.", how_to_check=""))
            log.append(f"Added missing context: {words}")

    return report, log
