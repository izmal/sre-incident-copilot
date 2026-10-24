"""Deterministic risk analysis: numbers and urgency are computed here, not by the LLM."""
from __future__ import annotations

from .schemas import NormalizedAlert, RiskAnalysis

GIB = 2**30


def analyze(alert: NormalizedAlert, policy: dict, required_context: list[str]) -> RiskAnalysis:
    m = alert.metrics
    r = RiskAnalysis()
    size, avail = m.get("filesystem_size_bytes"), m.get("filesystem_available_bytes")
    growth = m.get("growth_rate_bytes_per_hour")

    if size and avail is not None:
        r.available_pct = round(100 * avail / size, 2)
        r.used_pct = round(100 - r.available_pct, 2)
    elif m.get("used_pct") is not None:
        r.used_pct = float(m["used_pct"])
        r.available_pct = round(100 - r.used_pct, 2)
        if size:
            avail = size * r.available_pct / 100
    if avail is not None:
        r.available_gib = round(avail / GIB, 2)
    if growth is not None:
        r.growth_gib_per_hour = round(growth / GIB, 3)

    if avail is not None and growth is not None:
        if growth > 0:
            r.time_to_exhaustion_hours = round(avail / growth, 1)
            r.tte_note = "Linear estimate (available / net growth). Not a guaranteed failure time."
        else:
            r.tte_note = "Usage is flat or shrinking: no finite time-to-exhaustion under a linear model."
    elif growth is None:
        r.tte_note = "Growth rate unknown: time-to-exhaustion is NOT computed."
    else:
        r.tte_note = "Available space unknown: time-to-exhaustion is NOT computed."

    if m.get("inodes_free_pct") is not None:
        r.inode_free_pct = float(m["inodes_free_pct"])
    elif m.get("inodes_used_pct") is not None:
        r.inode_free_pct = round(100 - float(m["inodes_used_pct"]), 2)

    if m.get("used_pct") is not None and size and avail is not None:
        stated = float(m["used_pct"])
        if abs(stated - r.used_pct) > 3:
            r.warnings.append(f"Conflicting metrics: stated usage {stated}% vs computed {r.used_pct}% from size/available.")
    age = m.get("metrics_age_minutes")
    if age is not None and age > policy.get("stale_metrics_minutes", 30):
        r.warnings.append(f"Metrics are {age:.0f} min old: re-check before acting.")
    if m.get("deleted_open_files_bytes"):
        r.warnings.append(f"{m['deleted_open_files_bytes'] / GIB:.1f} GiB held by deleted-but-open files.")

    r.urgency, r.urgency_reasons = _urgency(r, m, policy)

    present = [k for k in required_context if m.get(k) is not None]
    r.missing_context = [k for k in required_context if m.get(k) is None]
    r.evidence_completeness = round(len(present) / len(required_context), 2) if required_context else 1.0
    return r


def _urgency(r: RiskAnalysis, m: dict, policy: dict) -> tuple[str, list[str]]:
    c, h, md = policy["critical"], policy["high"], policy["medium"]
    tte, avail_pct, inode_free = r.time_to_exhaustion_hours, r.available_pct, r.inode_free_pct
    growth = m.get("growth_rate_bytes_per_hour")

    if m.get("user_impact") is True:
        return "critical", ["User impact reported."]
    if tte is not None and tte <= c["tte_hours_max"]:
        return "critical", [f"TTE {tte}h <= {c['tte_hours_max']}h."]
    if avail_pct is not None and avail_pct <= c["available_pct_max"]:
        return "critical", [f"Only {avail_pct}% space left."]
    if inode_free is not None and inode_free <= c["inode_free_pct_max"]:
        return "critical", [f"Only {inode_free}% inodes free."]
    if tte is not None and tte <= h["tte_hours_max"]:
        return "high", [f"TTE {tte}h <= {h['tte_hours_max']}h."]
    if inode_free is not None and inode_free <= h["inode_free_pct_max"]:
        return "high", [f"Only {inode_free}% inodes free."]
    if growth is None:
        if avail_pct is not None and avail_pct <= h["available_pct_max_without_growth"]:
            return "high", [f"{avail_pct}% left and growth unknown."]
        if inode_free is not None:
            return "medium", [f"{inode_free}% inodes free, growth unknown."]
        return "unknown", ["Growth rate unknown: cannot rank urgency from a percentage alone."]
    if tte is not None and tte <= md["tte_hours_max"]:
        return "medium", [f"TTE {tte}h <= {md['tte_hours_max']}h."]
    if avail_pct is not None and growth <= 0 and avail_pct <= md["available_pct_max_when_stable"]:
        return "medium", [f"Stable usage but only {avail_pct}% headroom."]
    if avail_pct is None:
        return "unknown", ["Available space unknown."]
    return "low", ["Runway is longer than 72h or usage is stable." if tte else "Usage is stable."]
