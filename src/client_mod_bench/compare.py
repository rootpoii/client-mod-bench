"""The table behind `compare` (the HTML report uses it too)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import __version__
from .errors import ResultError
from .fmt import MISSING, fmt_bytes, fmt_count, fmt_delta, fmt_gb, fmt_pct, fmt_seconds
from .results import Group


@dataclass(frozen=True)
class MetricDef:
    key: str
    title: str
    fmt: Callable[[float | None], str]


METRICS: tuple[MetricDef, ...] = (
    MetricDef("idle_memory", "Idle RAM", fmt_bytes),
    MetricDef("renderer_memory", "Renderer RAM", fmt_bytes),
    MetricDef("peak_memory", "Peak RAM", fmt_bytes),
    MetricDef("js_heap_after_gc", "JS heap after GC", fmt_bytes),
    MetricDef("idle_cpu", "Idle CPU (one core)", fmt_pct),
    MetricDef("ready_time", "Launch to usable", fmt_seconds),
    MetricDef("settle_time", "Launch to idle", fmt_seconds),
    MetricDef("startup_cpu_seconds", "Startup CPU time", fmt_seconds),
    MetricDef("processes", "Processes", fmt_count),
)


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]


def _cell(metric: MetricDef, stat: dict[str, Any] | None, base: dict[str, Any] | None,
          spread: bool) -> str:
    if stat is None:
        return MISSING
    text = metric.fmt(stat["median"])
    if spread and stat.get("n", 1) > 1:
        text += f" ±{metric.fmt(stat['stdev'])}"
    if base is not None:
        delta = fmt_delta(stat["median"], base["median"])
        if delta:
            text += f" ({delta})"
    return text


def build_table(groups: Sequence[Group], baseline: str | None = None,
                spread: bool = False) -> Table:
    # metrics are rows, clients are columns; metrics nobody has data for are skipped
    if not groups:
        raise ResultError("nothing to compare")
    base: Group | None = None
    if baseline is not None:
        base = next((g for g in groups if g.label == baseline), None)
        if base is None:
            labels = ", ".join(g.label for g in groups)
            raise ResultError(f"baseline {baseline!r} not found; labels are: {labels}")
    header = ["Metric", *(f"{g.label} (baseline)" if g is base else g.label for g in groups)]
    rows: list[list[str]] = []
    for metric in METRICS:
        stats = [g.summary.get(metric.key) for g in groups]
        if all(stat is None for stat in stats):
            continue
        base_stat = base.summary.get(metric.key) if base is not None else None
        row = [metric.title]
        for group, stat in zip(groups, stats, strict=True):
            row.append(_cell(metric, stat, None if group is base else base_stat, spread))
        rows.append(row)
    rows.append(["Runs", *(str(len(g.runs)) for g in groups)])
    return Table(header, rows)


def render_markdown(table: Table) -> str:
    lines = [
        "| " + " | ".join(table.header) + " |",
        "| " + " | ".join([":---", *("---:" for _ in table.header[1:])]) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in table.rows)
    return "\n".join(lines)


def render_text(table: Table) -> str:
    everything = [table.header, *table.rows]
    widths = [max(len(row[i]) for row in everything) for i in range(len(table.header))]

    def line(row: list[str]) -> str:
        cells = [
            cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
            for i, cell in enumerate(row)
        ]
        return "  ".join(cells).rstrip()

    rule = "  ".join("-" * width for width in widths)
    return "\n".join([line(table.header), rule, *(line(row) for row in table.rows)])


def table_json(groups: Sequence[Group]) -> dict[str, Any]:
    return {
        "tool": {"name": "client-mod-bench", "version": __version__},
        "clients": [
            {"label": g.label, "runs": len(g.runs), "summary": g.summary, "system": g.system}
            for g in groups
        ],
    }


def footnote(groups: Sequence[Group]) -> str:
    counts = sorted({len(g.runs) for g in groups})
    runs = str(counts[0]) if len(counts) == 1 else f"{counts[0]}–{counts[-1]}"
    kinds = set().union(*(g.memory_metrics for g in groups)) if groups else {"uss"}
    if kinds == {"uss"}:
        memory = "unique set size (USS) summed over all client processes"
    elif kinds == {"rss"}:
        memory = "resident set size (RSS) summed over all client processes (USS was unavailable)"
    else:
        memory = "USS where available, RSS otherwise, summed over all client processes"
    system = groups[0].system if groups else {}
    os_name = " ".join(str(x) for x in (system.get("os"), system.get("os_release")) if x)
    machine_parts = [
        system.get("cpu"),
        f"{fmt_gb(system['memory_total'])} RAM" if system.get("memory_total") else None,
        os_name or None,
    ]
    machine = ", ".join(str(part) for part in machine_parts if part)
    return (
        f"Median of {runs} runs per client. RAM is the {memory}; CPU is a percentage of one "
        f"core. Measured with client-mod-bench {__version__}"
        + (f" on {machine}." if machine else ".")
    )
