"""Load version-controlled YAML expertise and pick the rules that apply to an alert."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .schemas import NormalizedAlert

EXPERTISE_DIR = Path(__file__).resolve().parent.parent / "expertise"


@dataclass
class Rule:
    id: str
    title: str
    file: str
    components: list[str]
    categories: list[str]
    rationale: str = ""
    required_context: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    dangerous_actions: list[dict] = field(default_factory=list)

    def applies(self, alert: NormalizedAlert) -> bool:
        comp_ok = "any" in self.components or alert.component in self.components
        cat_ok = "any" in self.categories or alert.category in self.categories
        return comp_ok and cat_ok

    def prompt_block(self) -> str:
        lines = [f"[{self.id}] {self.title}", f"  why: {' '.join(self.rationale.split())}"]
        lines += [f"  check: {c}" for c in self.checks]
        lines += [f"  constraint: {c}" for c in self.constraints]
        if self.dangerous_actions:
            lines.append("  dangerous (requires_approval=true, never a quick fix): "
                         + ", ".join(d["label"] for d in self.dangerous_actions))
        return "\n".join(lines)


@dataclass
class Expertise:
    rules: dict[str, Rule]
    urgency_policy: dict
    principles: list[str]

    def select(self, alert: NormalizedAlert) -> list[Rule]:
        return [r for r in self.rules.values() if r.applies(alert)]

    def danger_patterns(self) -> list[tuple[str, str, re.Pattern]]:
        return [(r.id, d["label"], re.compile(d["pattern"], re.I))
                for r in self.rules.values() for d in r.dangerous_actions]


@lru_cache(maxsize=1)
def load_expertise(directory: str = str(EXPERTISE_DIR)) -> Expertise:
    rules: dict[str, Rule] = {}
    policy: dict = {}
    principles: list[str] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        policy = doc.get("urgency", policy)
        principles = doc.get("principles", principles)
        for raw in doc.get("rules", []):
            applies = raw.get("applies_to", {})
            rules[raw["id"]] = Rule(
                id=raw["id"], title=raw["title"], file=path.name,
                components=applies.get("components", ["any"]), categories=applies.get("categories", ["any"]),
                rationale=raw.get("rationale", ""), required_context=raw.get("required_context", []),
                checks=raw.get("checks", []), constraints=raw.get("constraints", []),
                dangerous_actions=raw.get("dangerous_actions", []),
            )
    return Expertise(rules=rules, urgency_policy=policy, principles=principles)


def required_context(rules: list[Rule]) -> list[str]:
    seen: list[str] = []
    for r in rules:
        for k in r.required_context:
            if k not in seen:
                seen.append(k)
    return seen
