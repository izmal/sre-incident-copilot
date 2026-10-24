"""Data contracts: normalized alert, deterministic risk analysis, LLM report."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Urgency = Literal["critical", "high", "medium", "low", "unknown"]
URGENCY_ORDER = ["low", "medium", "high", "critical"]


class Fact(BaseModel):
    name: str
    value: Any
    source: str  # observed_metrics | labels | annotation text | free text | computed


class NormalizedAlert(BaseModel):
    source: str = "manual"
    alert_names: list[str] = []
    component: str = "unknown"  # linux | kubernetes | mongodb | unknown
    category: str = "disk_pressure"  # disk_pressure | inode_exhaustion
    status: str = "firing"
    severity_label: str | None = None  # kept separate from computed urgency
    instance: str | None = None
    environment: str | None = None
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    metrics: dict[str, Any] = {}  # canonical metric name -> value
    facts: list[Fact] = []
    free_text: str = ""
    alert_count: int = 1


class RiskAnalysis(BaseModel):
    used_pct: float | None = None
    available_pct: float | None = None
    available_gib: float | None = None
    growth_gib_per_hour: float | None = None
    time_to_exhaustion_hours: float | None = None
    tte_note: str = ""
    inode_free_pct: float | None = None
    urgency: Urgency = "unknown"
    urgency_reasons: list[str] = []
    missing_context: list[str] = []
    warnings: list[str] = []
    evidence_completeness: float = 0.0  # share of required context present


# ---- LLM output contract (same schema for baseline and expert mode) ----

def _to_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return "; ".join(f"{k}: {x}" for k, x in v.items())
    return str(v)


class Lenient(BaseModel):
    """Small models drift on types; coerce scalars instead of failing the demo."""

    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, v, info):
        field = cls.model_fields[info.field_name]
        if field.annotation is str and v is not None and not isinstance(v, str):
            return _to_str(v)
        if field.annotation is bool and isinstance(v, str):
            return v.strip().lower() in {"true", "yes", "1", "required"}
        return v


class MissingItem(Lenient):
    item: str
    why: str = ""
    how_to_check: str = ""


class Action(Lenient):
    action: str
    purpose: str = ""
    conditions: str = ""
    expected_effect: str = ""
    risks: str = ""
    requires_approval: bool = False
    verify: str = ""
    rule_ids: list[str] = []
    guard_note: str = ""  # filled by the output validator, never by the LLM


class Step(Lenient):
    step: str
    hypothesis: str = ""
    how_to_check: str = ""


class DangerousOp(Lenient):
    operation: str
    why_dangerous: str = ""
    rule_id: str = ""


class Assessment(Lenient):
    urgency: str = "unknown"
    rationale: str = ""


class Incident(Lenient):
    component: str = "unknown"
    category: str = "unknown"
    status: str = "firing"
    summary: str = ""


class Report(Lenient):
    incident: Incident = Field(default_factory=Incident)
    assessment: Assessment = Field(default_factory=Assessment)
    facts: list[str] = []
    missing_context: list[MissingItem] = []
    immediate_actions: list[Action] = []
    investigation_steps: list[Step] = []
    root_cause_hypotheses: list[str] = []
    dangerous_operations: list[DangerousOp] = []
    applied_expert_rules: list[str] = []
    incident_update: str = ""

    @field_validator("facts", "root_cause_hypotheses", mode="before")
    @classmethod
    def _str_list(cls, v):
        return [_to_str(x) for x in v] if isinstance(v, list) else v

    @field_validator("missing_context", mode="before")
    @classmethod
    def _missing(cls, v):
        if isinstance(v, list):
            return [{"item": x} if isinstance(x, str) else x for x in v]
        return v

    @field_validator("investigation_steps", mode="before")
    @classmethod
    def _steps(cls, v):
        if isinstance(v, list):
            return [{"step": x} if isinstance(x, str) else x for x in v]
        return v

    @field_validator("immediate_actions", mode="before")
    @classmethod
    def _actions(cls, v):
        if isinstance(v, list):
            return [{"action": x} if isinstance(x, str) else x for x in v]
        return v

    @field_validator("dangerous_operations", mode="before")
    @classmethod
    def _danger(cls, v):
        if isinstance(v, list):
            return [{"operation": x} if isinstance(x, str) else x for x in v]
        return v


REPORT_JSON_SHAPE = """{
  "incident": {"component": "...", "category": "...", "status": "firing|resolved", "summary": "one sentence"},
  "assessment": {"urgency": "critical|high|medium|low|unknown", "rationale": "why"},
  "facts": ["only facts present in the input"],
  "missing_context": [{"item": "...", "why": "...", "how_to_check": "command or query"}],
  "immediate_actions": [{"action": "...", "purpose": "...", "conditions": "...", "expected_effect": "...",
                         "risks": "...", "requires_approval": true, "verify": "how to verify", "rule_ids": ["..."]}],
  "investigation_steps": [{"step": "...", "hypothesis": "...", "how_to_check": "command or query"}],
  "root_cause_hypotheses": ["..."],
  "dangerous_operations": [{"operation": "...", "why_dangerous": "...", "rule_id": "..."}],
  "applied_expert_rules": ["rule ids"],
  "incident_update": "short Slack-ready message for the team"
}"""
