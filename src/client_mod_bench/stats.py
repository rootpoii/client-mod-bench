"""median/mean/stdev that skip None instead of blowing up."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def _clean(values: Iterable[float | None]) -> list[float]:
    return [float(v) for v in values if v is not None]


def mean(values: Iterable[float | None]) -> float | None:
    xs = _clean(values)
    return sum(xs) / len(xs) if xs else None


def median(values: Iterable[float | None]) -> float | None:
    xs = sorted(_clean(values))
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def stdev(values: Iterable[float | None]) -> float | None:
    # sample stdev (n - 1); a single value gives 0 rather than an error
    xs = _clean(values)
    if not xs:
        return None
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def summarize(values: Iterable[float | None]) -> dict[str, Any] | None:
    xs = _clean(values)
    if not xs:
        return None
    return {
        "median": median(xs),
        "mean": mean(xs),
        "min": min(xs),
        "max": max(xs),
        "stdev": stdev(xs),
        "n": len(xs),
    }
