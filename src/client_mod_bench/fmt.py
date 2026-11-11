"""Formatting for the numbers that end up in tables and reports."""

from __future__ import annotations

import re

MIB = 1024 * 1024
GIB = 1024 * MIB
MISSING = "–"


def fmt_bytes(value: float | None) -> str:
    # 1024-based, same as Task Manager and Activity Monitor
    if value is None:
        return MISSING
    mib = value / MIB
    if abs(mib) >= 1024:
        return f"{mib / 1024:.2f} GB"
    if abs(mib) >= 100:
        return f"{mib:.0f} MB"
    return f"{mib:.1f} MB"


def fmt_gb(value: float | None) -> str:
    if value is None:
        return MISSING
    return f"{value / GIB:.0f} GB"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return MISSING
    return f"{value:.2f}%"


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return MISSING
    return f"{value:.2f} s"


def fmt_count(value: float | None) -> str:
    if value is None:
        return MISSING
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def fmt_delta(value: float | None, base: float | None) -> str:
    """e.g. "+3.9%". Empty when there is nothing to compare against."""
    if value is None or base is None or base == 0:
        return ""
    return f"{(value - base) / abs(base) * 100:+.1f}%"


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.").lower()
    return slug or "result"
