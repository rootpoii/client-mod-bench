"""Single-file HTML report (no JS, no external requests): table plus memory/CPU charts."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import datetime
from html import escape
from typing import Any

from . import __version__
from .compare import build_table, footnote
from .fmt import MIB, fmt_gb
from .results import Group, environment_warnings
from .stats import median

Point = tuple[float, float]
Series = tuple[int, str, list[Point]]
_SERIES_COLORS = 8

_CSS = """
:root{--bg:#ffffff;--fg:#141821;--muted:#5f6878;--border:#e3e6eb;--grid:#eef0f4;
--panel:#f7f8fa;--warn-bg:#fff6e8;--warn-fg:#8a4b08;
--s0:#2563eb;--s1:#dc2626;--s2:#059669;--s3:#d97706;--s4:#7c3aed;--s5:#db2777;
--s6:#0891b2;--s7:#4b5563}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1116;--fg:#e6e8ec;
--muted:#9aa3b2;--border:#272c36;--grid:#1b1f27;--panel:#151920;--warn-bg:#2b1d0c;
--warn-fg:#f6c47a;--s0:#60a5fa;--s1:#f87171;--s2:#34d399;--s3:#fbbf24;--s4:#a78bfa;
--s5:#f472b6;--s6:#22d3ee;--s7:#9ca3af}}
:root[data-theme="dark"]{--bg:#0f1116;--fg:#e6e8ec;--muted:#9aa3b2;--border:#272c36;
--grid:#1b1f27;--panel:#151920;--warn-bg:#2b1d0c;--warn-fg:#f6c47a;--s0:#60a5fa;
--s1:#f87171;--s2:#34d399;--s3:#fbbf24;--s4:#a78bfa;--s5:#f472b6;--s6:#22d3ee;--s7:#9ca3af}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:40px 16px 64px}
h1{font-size:28px;line-height:1.2;margin:0 0 6px;letter-spacing:-0.01em}
h2{font-size:17px;margin:40px 0 12px}
.muted{color:var(--muted)}.small{font-size:13px}
.warn{background:var(--warn-bg);color:var(--warn-fg);border-radius:8px;padding:10px 14px;
margin:20px 0 0;font-size:14px}
.warn p{margin:4px 0}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:9px 14px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
thead th{background:var(--panel);font-weight:600}
tbody tr:last-child td{border-bottom:0;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 8px;font-size:14px}
.legend span{display:inline-flex;align-items:center;gap:7px}
.legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
.chart{border:1px solid var(--border);border-radius:10px;padding:10px 6px 4px}
svg{display:block;width:100%;height:auto}
.grid{stroke:var(--grid);stroke-width:1}
.tick{fill:var(--muted);font-size:11px}
.marker{stroke:var(--muted);stroke-dasharray:4 4;stroke-width:1}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 18px;margin:0;font-size:14px}
dt{color:var(--muted)}dd{margin:0}
"""


def _color_rules() -> str:
    rules = [f".s{i}{{stroke:var(--s{i})}}.k{i}{{background:var(--s{i})}}" for i in
             range(_SERIES_COLORS)]
    return "".join(rules)


def _nice_step(raw: float) -> float:
    if raw <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if multiple * magnitude >= raw - 1e-12:
            return multiple * magnitude
    return 10 * magnitude


def representative_run(group: Group) -> dict[str, Any] | None:
    """The run whose idle memory is closest to the group median."""
    runs = [run for run in group.runs if run.get("samples")]
    if not runs:
        return None
    target = median(run.get("metrics", {}).get("idle_memory") for run in runs)
    if target is None:
        return runs[0]
    return min(runs, key=lambda run: abs((run["metrics"].get("idle_memory") or 0) - target))


def memory_points(run: dict[str, Any]) -> list[Point]:
    points = []
    for s in run["samples"]:
        if s.get("procs", 0):
            memory = s["uss"] if s.get("uss") is not None else s["rss"]
            points.append((float(s["t"]), memory / MIB))
    return points


def cpu_points(run: dict[str, Any], window: float = 5.0) -> list[Point]:
    # per-second CPU is way too spiky to read, so average over the last few seconds
    samples = run["samples"]
    points = []
    for i, s in enumerate(samples):
        work = span = 0.0
        j = i
        while j >= 0 and span < window:
            work += samples[j]["cpu"] * samples[j]["dt"]
            span += samples[j]["dt"]
            j -= 1
        points.append((float(s["t"]), work / span if span > 0 else 0.0))
    return points


def svg_chart(series: Sequence[Series], *, unit: str, marker: float | None,
              width: int = 760, height: int = 260) -> str:
    left, right, top, bottom = 58, 14, 12, 30
    xs = [x for _, _, points in series for x, _ in points]
    ys = [y for _, _, points in series for _, y in points]
    if not xs:
        return '<p class="muted">No samples were recorded.</p>'
    x_max = max(xs) or 1.0
    y_step = _nice_step(max(max(ys), 1e-9) / 4)
    y_max = y_step * max(1, math.ceil(max(ys) * 1.05 / y_step))
    plot_w = width - left - right
    plot_h = height - top - bottom

    def sx(x: float) -> float:
        return left + x / x_max * plot_w

    def sy(y: float) -> float:
        return top + plot_h - y / y_max * plot_h

    out = [f'<svg viewBox="0 0 {width} {height}" role="img">']
    value = 0.0
    while value <= y_max + 1e-9:
        y = sy(value)
        out.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" '
                   f'y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">'
                   f'{value:.10g} {escape(unit)}</text>')
        value += y_step
    x_step = _nice_step(x_max / 6)
    value = 0.0
    while value <= x_max + 1e-9:
        out.append(f'<text class="tick" x="{sx(value):.1f}" y="{height - 8}" '
                   f'text-anchor="middle">{value:.10g}s</text>')
        value += x_step
    if marker is not None and 0 < marker < x_max:
        x = sx(marker)
        out.append(f'<line class="marker" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" '
                   f'y2="{top + plot_h}"/>')
        out.append(f'<text class="tick" x="{x + 6:.1f}" y="{top + 12}">idle window</text>')
    for index, label, points in series:
        coords = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
        out.append(f'<polyline class="line s{index % _SERIES_COLORS}" points="{coords}">'
                   f"<title>{escape(label)}</title></polyline>")
    out.append("</svg>")
    return "".join(out)


def _legend(series: Sequence[Series]) -> str:
    items = "".join(
        f'<span><i class="k{index % _SERIES_COLORS}"></i>{escape(label)}</span>'
        for index, label, _ in series
    )
    return f'<div class="legend">{items}</div>'


def _series(groups: Sequence[Group], points: Callable[[dict[str, Any]], list[Point]]
            ) -> list[Series]:
    series: list[Series] = []
    for index, group in enumerate(groups):
        run = representative_run(group)
        if run is not None:
            series.append((index, group.label, points(run)))
    return series


def _table_html(groups: Sequence[Group], baseline: str | None) -> str:
    table = build_table(groups, baseline=baseline, spread=True)
    head = "".join(f"<th>{escape(cell)}</th>" for cell in table.header)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
        for row in table.rows
    )
    return (f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def _system_html(system: dict[str, Any]) -> str:
    os_name = " ".join(str(x) for x in (system.get("os"), system.get("os_release")) if x)
    cores = f"{system.get('physical_cpus', '?')} cores / {system.get('logical_cpus', '?')} threads"
    rows = [
        ("CPU", system.get("cpu")),
        ("Cores", cores),
        ("Memory", fmt_gb(system.get("memory_total"))),
        ("OS", os_name or None),
        ("Python / psutil", f"{system.get('python', '?')} / {system.get('psutil', '?')}"),
    ]
    items = "".join(
        f"<dt>{escape(name)}</dt><dd>{escape(str(value))}</dd>" for name, value in rows if value
    )
    return f"<dl>{items}</dl>"


def render_report(groups: Sequence[Group], baseline: str | None = None,
                  title: str = "Client benchmark") -> str:
    warnings = environment_warnings(groups)
    warmups = {g.config.get("warmup") for g in groups}
    marker = next(iter(warmups)) if len(warmups) == 1 else None
    memory = _series(groups, memory_points)
    cpu = _series(groups, cpu_points)
    warn_html = ""
    if warnings:
        warn_html = '<div class="warn">' + "".join(
            f"<p>{escape(w)}</p>" for w in warnings) + "</div>"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    labels = ", ".join(g.label for g in groups)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{_CSS}{_color_rules()}</style>
</head>
<body>
<main>
<h1>{escape(title)}</h1>
<p class="muted">{escape(labels)} · generated {generated} · client-mod-bench {__version__}</p>
{warn_html}
<h2>Results</h2>
{_table_html(groups, baseline)}
<p class="muted small">{escape(footnote(groups))}</p>
<h2>Memory over time</h2>
<p class="muted small">Representative run per client (closest to the median idle RAM).</p>
{_legend(memory)}<div class="chart">{svg_chart(memory, unit="MB", marker=marker)}</div>
<h2>CPU over time</h2>
<p class="muted small">Percent of one core, averaged over the previous 5 seconds.</p>
{_legend(cpu)}<div class="chart">{svg_chart(cpu, unit="%", marker=marker)}</div>
<h2>Test system</h2>
{_system_html(groups[0].system if groups else {})}
</main>
</body>
</html>
"""
