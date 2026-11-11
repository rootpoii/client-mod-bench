"""Turns raw samples into per-run numbers. No I/O in here, which keeps it easy to test.

A sample (see ProcessTracker.sample) looks like this:

    {"t": 12.0,      # seconds since launch / attach
     "dt": 1.0,      # length of the interval "cpu" covers, ending at t
     "cpu": 35.2,    # % of one core, a fully busy core is 100
     "rss": ...,     # bytes, summed over all client processes
     "uss": ...,     # same, or None when it couldn't be read
     "procs": 6,
     "by_type": {"browser": ..., "renderer": ..., "gpu": ...}}
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .stats import median

Sample = dict[str, Any]
_EPS = 1e-9


def _interval_start(sample: Sample) -> float:
    return float(sample["t"]) - float(sample["dt"])


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def weighted_cpu(samples: Sequence[Sample]) -> float | None:
    # weighted by dt, the intervals aren't always exactly the same length
    total = sum(s["dt"] for s in samples)
    if total <= 0:
        return None
    return sum(s["cpu"] * s["dt"] for s in samples) / total


def settle_time(samples: Sequence[Sample], threshold: float, window: float) -> float | None:
    """Launch to idle: start of the first `window` seconds where mean CPU <= threshold.

    None if the client never calmed down during the run.
    """
    start = 0
    for end in range(len(samples)):
        while (
            start < end
            and samples[end]["t"] - _interval_start(samples[start + 1]) >= window - _EPS
        ):
            start += 1
        span_start = _interval_start(samples[start])
        if samples[end]["t"] - span_start < window - _EPS:
            continue
        cpu = weighted_cpu(samples[start:end + 1])
        if cpu is not None and cpu <= threshold:
            return round(span_start, 3)
    return None


def startup_cpu_seconds(samples: Sequence[Sample], until: float) -> float:
    """Core-seconds of CPU used from launch until `until`."""
    used = sum(s["cpu"] / 100.0 * s["dt"] for s in samples if s["t"] <= until + _EPS)
    return round(used, 3)


def compute_run_metrics(
    samples: Sequence[Sample],
    *,
    warmup: float,
    settle_cpu: float = 5.0,
    settle_window: float = 5.0,
    launched: bool = True,
) -> dict[str, Any]:
    # the "idle" numbers only look at samples after warmup
    alive = [s for s in samples if s.get("procs", 0) > 0]
    if not alive:
        return {}
    idle = [s for s in alive if s["t"] >= warmup - _EPS]
    idle_cpu = [s for s in samples if _interval_start(s) >= warmup - _EPS]

    # never mix USS and RSS in one median: if any sample is missing USS, the whole run
    # uses RSS
    key = "uss" if all(s.get("uss") is not None for s in alive) else "rss"
    kinds = sorted({kind for s in idle for kind in s.get("by_type", {})})
    by_type = {kind: median(s.get("by_type", {}).get(kind, 0) for s in idle) for kind in kinds}

    metrics: dict[str, Any] = {
        "memory_metric": key,
        "idle_memory": median(s[key] for s in idle),
        "idle_rss": median(s["rss"] for s in idle),
        "peak_memory": max(s[key] for s in alive),
        "renderer_memory": by_type.get("renderer"),
        "idle_memory_by_type": by_type,
        "idle_cpu": _round(weighted_cpu(idle_cpu), 3),
        "processes": median(s["procs"] for s in idle),
    }
    if launched:
        settled = settle_time(samples, settle_cpu, settle_window)
        metrics["settle_time"] = settled
        metrics["startup_cpu_seconds"] = (
            startup_cpu_seconds(samples, settled) if settled is not None else None
        )
    return metrics
