"""Turn Alertmanager JSON, a simple JSON object or free text into NormalizedAlert."""
from __future__ import annotations

import json
import re
from typing import Any

from .schemas import Fact, NormalizedAlert

UNIT = {
    "b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
    "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
    "k": 2**10, "m": 2**20, "g": 2**30, "t": 2**40, "ki": 2**10, "mi": 2**20, "gi": 2**30, "ti": 2**40,
}
PER_HOUR = {"s": 3600, "sec": 3600, "m": 60, "min": 60, "minute": 60, "h": 1, "hr": 1, "hour": 1, "d": 1 / 24, "day": 1 / 24}
SIZE_RE = r"(-?\d+(?:\.\d+)?)\s*([KMGT]i?B?|B)\b"

BYTE_KEYS = {"filesystem_size_bytes", "filesystem_available_bytes", "deleted_open_files_bytes"}
BOOL_KEYS = {"user_impact", "allow_volume_expansion"}
# canonical metric names the analyzer and rules understand
ALIASES = {
    "size": "filesystem_size_bytes", "filesystem_size": "filesystem_size_bytes", "capacity": "filesystem_size_bytes",
    "available": "filesystem_available_bytes", "filesystem_available": "filesystem_available_bytes", "free": "filesystem_available_bytes",
    "growth_rate": "growth_rate_bytes_per_hour", "growth_rate_per_hour": "growth_rate_bytes_per_hour", "net_growth": "growth_rate_bytes_per_hour",
    "used_pct": "used_pct", "usage_pct": "used_pct", "disk_utilization_pct": "used_pct",
    "role": "replica_set_role", "replica_role": "replica_set_role",
    "allowvolumeexpansion": "allow_volume_expansion",
}


def parse_bytes(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*" + SIZE_RE + r"\s*", str(v), re.I)
    if not m:
        try:
            return float(v)
        except ValueError:
            return None
    return float(m.group(1)) * _unit(m.group(2))


def _unit(u: str) -> float:
    u = u.lower()
    return UNIT.get(u, UNIT.get(u.rstrip("b"), 1))


def parse_rate_per_hour(v: Any) -> float | None:
    """'5GiB/h', '120 MiB/min', 5368709120 (bytes/hour) -> bytes per hour."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*" + SIZE_RE + r"\s*(?:/|per\s*)?\s*(s|sec|m|min|minute|h|hr|hour|d|day)?s?\s*", str(v), re.I)
    if not m:
        return parse_bytes(v)
    per = PER_HOUR[(m.group(3) or "h").lower()]
    return float(m.group(1)) * _unit(m.group(2)) * per


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    return str(v).strip().lower() in {"true", "yes", "1"}


def _canonical(metrics: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in metrics.items():
        key = ALIASES.get(k.lower(), k.lower())
        if key in BYTE_KEYS:
            v = parse_bytes(v)
        elif key == "growth_rate_bytes_per_hour":
            v = parse_rate_per_hour(v)
        elif key in BOOL_KEYS:
            v = _to_bool(v)
        elif key in {"used_pct", "inodes_free_pct", "inodes_used_pct", "replication_lag_seconds", "metrics_age_minutes"}:
            v = float(str(v).rstrip("%s "))
        out[key] = v
    return out


# --- text extraction: best effort, explicit metrics always win ---
TEXT_PATTERNS = [
    ("filesystem_size_bytes", r"(?:size|total|capacity)\W{0,3}(?:of\s+|is\s+|=\s*)?" + SIZE_RE, parse_bytes),
    ("filesystem_size_bytes", r"(?:available|free|left)\s+(?:of|out of)\s+" + SIZE_RE, parse_bytes),
    ("filesystem_available_bytes", SIZE_RE + r"\s+(?:available|free|left)\b", parse_bytes),
    ("filesystem_available_bytes", r"(?:avail\w*|free)\W{0,3}(?:space\s+)?(?:is\s+|=\s*)?" + SIZE_RE, parse_bytes),
    ("growth_rate_bytes_per_hour", r"(?:\+|grow\w*\s+(?:by\s+|at\s+)?|growth\W{0,3}(?:of\s+)?)?" + SIZE_RE + r"\s*(?:/|per\s+)(s|sec|min|minute|h|hr|hour|d|day)\b", None),
    ("used_pct", r"(\d+(?:\.\d+)?)\s*%\s*(?:used|full|utili[sz]ed|utili[sz]ation)", float),
    ("used_pct", r"(?:usage|utili[sz]ation|used)\W{0,3}(?:is\s+|at\s+)?(\d+(?:\.\d+)?)\s*%", float),
    ("inodes_used_pct", r"inodes?\W{0,12}(?:usage\s+|used\s+)?(?:is\s+|at\s+)?(\d+(?:\.\d+)?)\s*%\s*(?:used)?", float),
    ("inodes_free_pct", r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?inodes?\s+(?:free|left|available)", float),
]


def extract_from_text(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    # inode patterns first, then blank them out so "inodes 99% used" is not read as disk usage
    ordered = sorted(TEXT_PATTERNS, key=lambda p: not p[0].startswith("inodes"))
    for key, pat, conv in ordered:
        if key in found:
            continue
        m = re.search(pat, text, re.I)
        if not m:
            continue
        if key.startswith("inodes"):
            text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
        if key == "growth_rate_bytes_per_hour":
            found[key] = float(m.group(1)) * _unit(m.group(2)) * PER_HOUR[m.group(3).lower()]
        elif conv is parse_bytes:
            found[key] = float(m.group(1)) * _unit(m.group(2))
        else:
            found[key] = conv(m.group(1))
    role = re.search(r"\b(primary|secondary)\b", text, re.I)
    if role:
        found["replica_set_role"] = role.group(1).lower()
    return found


def detect_component(names: list[str], labels: dict[str, str], text: str) -> str:
    blob = " ".join(names + [f"{k}={v}" for k, v in labels.items()] + [text]).lower()
    if "mongo" in blob:
        return "mongodb"
    if any(w in blob for w in ("persistentvolume", "pvc", "kube", "statefulset", "namespace=")):
        return "kubernetes"
    if any(w in blob for w in ("node_filesystem", "nodefilesystem", "filesystem", "disk", "mountpoint", "inode", "/var", "df ")):
        return "linux"
    return "unknown"


def detect_category(names: list[str], text: str, metrics: dict[str, Any]) -> str:
    blob = (" ".join(names) + " " + text).lower()
    if "inode" in blob or "filesalmostout" in blob or {"inodes_free_pct", "inodes_used_pct"} & metrics.keys():
        return "inode_exhaustion"
    return "disk_pressure"


def normalize(raw: str | dict) -> NormalizedAlert:
    """Accepts Alertmanager webhook JSON, {"alerts": [...], "observed_metrics": {...}, "context": "..."},
    a single alert object, or plain text."""
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"context": raw}
    if not isinstance(data, dict):
        data = {"context": str(data)}

    alerts = data.get("alerts") or ([data] if "labels" in data else [])
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    names: list[str] = []
    statuses = []
    for a in alerts:
        lab = {k: str(v) for k, v in (a.get("labels") or {}).items()}
        labels.update({k: v for k, v in lab.items() if k not in labels})
        for k, v in (a.get("annotations") or {}).items():
            annotations[k] = (annotations[k] + "\n" + str(v)) if k in annotations else str(v)
        if lab.get("alertname"):
            names.append(lab["alertname"])
        statuses.append(a.get("status", data.get("status", "firing")))
    labels.update({k: str(v) for k, v in (data.get("commonLabels") or {}).items() if k not in labels})

    free_text = str(data.get("context") or "")
    ann_text = "\n".join(annotations.values())

    facts: list[Fact] = []
    metrics: dict[str, Any] = {}
    for src, text in (("free text", free_text), ("annotation text", ann_text)):
        for k, v in extract_from_text(text).items():
            metrics[k] = v
            facts.append(Fact(name=k, value=v, source=src))
    explicit = _canonical(data.get("observed_metrics") or {})
    for k, v in explicit.items():
        metrics[k] = v
        facts.append(Fact(name=k, value=v, source="observed_metrics"))
    # keep only the winning source per metric
    winners = {}
    for f in facts:
        winners[f.name] = f
    facts = list(winners.values())

    instance = labels.get("instance") or labels.get("persistentvolumeclaim")
    for k in ("alertname", "instance", "severity", "namespace", "persistentvolumeclaim", "mountpoint", "job"):
        if k in labels:
            facts.append(Fact(name=f"label.{k}", value=labels[k], source="labels"))

    component = detect_component(names, labels, ann_text + " " + free_text)
    return NormalizedAlert(
        source="alertmanager" if "alerts" in data else ("json" if alerts else "text"),
        alert_names=names,
        component=component,
        category=detect_category(names, ann_text + " " + free_text, metrics),
        status="firing" if not statuses or "firing" in statuses else statuses[0],
        severity_label=labels.get("severity"),
        instance=instance,
        environment=labels.get("env") or labels.get("environment"),
        labels=labels,
        annotations=annotations,
        metrics=metrics,
        facts=facts,
        free_text=free_text,
        alert_count=max(1, len(alerts)),
    )
