"""Result JSON files: building, saving, loading, grouping by label."""

from __future__ import annotations

import json
import platform
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from . import __version__
from .errors import ResultError
from .fmt import slugify
from .stats import summarize

SCHEMA_VERSION = 1
SUMMARY_KEYS = (
    "idle_memory",
    "idle_rss",
    "peak_memory",
    "renderer_memory",
    "js_heap_after_gc",
    "idle_cpu",
    "ready_time",
    "settle_time",
    "startup_cpu_seconds",
    "processes",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cpu_model() -> str:
    system = platform.system()
    try:
        if system == "Windows":
            import winreg

            key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if system == "Linux":
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        if system == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.stdout.strip():
                return out.stdout.strip()
    except (OSError, ImportError, subprocess.SubprocessError):
        pass  # the CPU name is nice to have, never worth crashing a run over
    return platform.processor() or "unknown"


def system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "cpu": cpu_model(),
        "logical_cpus": psutil.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "memory_total": psutil.virtual_memory().total,
        "python": platform.python_version(),
        "psutil": psutil.__version__,
    }
    battery = None
    sensors_battery = getattr(psutil, "sensors_battery", None)
    if sensors_battery is not None:
        try:
            battery = sensors_battery()
        except (OSError, RuntimeError, NotImplementedError):
            battery = None
    info["on_battery"] = bool(battery is not None and not battery.power_plugged)
    return info


def summarize_runs(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    runs = list(runs)
    summary: dict[str, Any] = {}
    for key in SUMMARY_KEYS:
        stats = summarize(run.get("metrics", {}).get(key) for run in runs)
        if stats is not None:
            summary[key] = stats
    return summary


def build_result(
    *,
    label: str,
    mode: str,
    target: dict[str, Any],
    config: dict[str, Any],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "tool": {"name": "client-mod-bench", "version": __version__},
        "label": label,
        "mode": mode,
        "created_at": now_iso(),
        "target": target,
        "config": config,
        "system": system_info(),
        "summary": summarize_runs(runs),
        "runs": runs,
    }


def save_result(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{slugify(result['label'])}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    path = out_dir / f"{stem}.json"
    counter = 2
    while path.exists():
        path = out_dir / f"{stem}-{counter}.json"
        counter += 1
    path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return path


def load_results(paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    """Takes files and directories (every *.json inside)."""
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            files.append(path)
        else:
            raise ResultError(f"{path}: no such file or directory")
    results = []
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ResultError(f"{file}: {err}") from err
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION or "runs" not in data:
            raise ResultError(f"{file}: not a client-mod-bench result file")
        data["_path"] = str(file)
        results.append(data)
    if not results:
        raise ResultError("no result files found")
    # oldest first, so columns come out in the order things were measured
    results.sort(key=lambda result: str(result.get("created_at", "")))
    return results


@dataclass
class Group:
    """Every run with the same label, possibly from several result files."""

    label: str
    results: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def system(self) -> dict[str, Any]:
        return self.results[0].get("system", {}) if self.results else {}

    @property
    def config(self) -> dict[str, Any]:
        return self.results[0].get("config", {}) if self.results else {}

    @property
    def memory_metrics(self) -> set[str]:
        return {run.get("metrics", {}).get("memory_metric", "uss") for run in self.runs}


def group_by_label(results: Iterable[dict[str, Any]]) -> list[Group]:
    groups: dict[str, Group] = {}
    for result in results:
        label = str(result.get("label") or "unnamed")
        group = groups.setdefault(label, Group(label))
        group.results.append(result)
        group.runs.extend(result.get("runs", []))
    for group in groups.values():
        group.summary = summarize_runs(group.runs)
    return list(groups.values())


def order_groups(groups: list[Group], order: Sequence[str] | None,
                 baseline: str | None = None) -> list[Group]:
    # --order wins; without it the baseline goes first
    if not order:
        return sorted(groups, key=lambda group: group.label != baseline) if baseline else groups
    by_label = {group.label: group for group in groups}
    unknown = [label for label in order if label not in by_label]
    if unknown:
        raise ResultError(
            f"unknown label(s) in --order: {', '.join(unknown)}; "
            f"labels are: {', '.join(by_label)}"
        )
    return [by_label[label] for label in order] + [g for g in groups if g.label not in order]


def environment_warnings(groups: Iterable[Group]) -> list[str]:
    """Things that would make a comparison apples to oranges."""
    groups = list(groups)
    warnings: list[str] = []
    machines: dict[tuple[Any, ...], set[str]] = {}
    timings: set[tuple[Any, ...]] = set()
    modes: set[Any] = set()
    for group in groups:
        for result in group.results:
            system = result.get("system", {})
            machine = (system.get("cpu"), system.get("os"), system.get("os_release"))
            machines.setdefault(machine, set()).add(group.label)
            config = result.get("config", {})
            timings.add((config.get("duration"), config.get("warmup"), config.get("interval")))
            modes.add(result.get("mode"))
            if system.get("on_battery"):
                warnings.append(f"{group.label}: measured on battery power")
            incomplete = result.get("incomplete")
            if isinstance(incomplete, dict):
                warnings.append(
                    f"{group.label}: a session stopped early ({incomplete.get('completed_runs')} "
                    f"of {incomplete.get('requested_runs')} runs)"
                )
    if len(machines) > 1:
        where = "; ".join(
            f"{cpu} / {os_name} {release}: {', '.join(sorted(labels))}"
            for (cpu, os_name, release), labels in machines.items()
        )
        warnings.append(f"results come from different machines or OS versions ({where})")
    if len(timings) > 1:
        warnings.append("results were recorded with different --duration/--warmup/--interval")
    if len(modes) > 1:
        warnings.append("launch and attach results are mixed; startup metrics need launch runs")
    return list(dict.fromkeys(warnings))
