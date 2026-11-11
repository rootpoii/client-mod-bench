"""Launch (or attach to) a client, sample it, and turn the samples into a result file."""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

from .cdp import DEFAULT_READY_SELECTOR, CdpProbe
from .errors import BenchError
from .fmt import fmt_bytes, fmt_pct, fmt_seconds
from .metrics import compute_run_metrics
from .results import build_result, now_iso, save_result
from .targets import LaunchSpec, refresh_target
from .tracker import ProcessTracker, find_running, terminate


@dataclass
class RunConfig:
    duration: float = 90.0
    warmup: float = 30.0
    interval: float = 1.0
    runs: int = 3
    discard_first: bool = False
    cooldown: float = 10.0
    settle_cpu: float = 5.0
    settle_window: float = 5.0
    collect_uss: bool = True
    cdp: bool = False
    cdp_port: int = 0
    cdp_url: str = "discord.com"
    ready_selector: str = DEFAULT_READY_SELECTOR
    kill_existing: bool = False
    exit_grace: float = 8.0


class Progress:
    """One live status line on a terminal; an occasional plain line when output is redirected."""

    def __init__(self, quiet: bool = False, stream: TextIO | None = None) -> None:
        self.quiet = quiet
        self.stream = stream if stream is not None else sys.stderr
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._width = 0
        self._last_plain = float("-inf")

    def update(self, text: str) -> None:
        if self.quiet:
            return
        if self.tty:
            self.stream.write("\r" + text.ljust(self._width))
            self.stream.flush()
            self._width = len(text)
        elif time.monotonic() - self._last_plain >= 10:
            self._last_plain = time.monotonic()
            print(text, file=self.stream, flush=True)

    def line(self, text: str) -> None:
        if self.quiet:
            return
        self.clear()
        print(text, file=self.stream, flush=True)

    def clear(self) -> None:
        if self.tty and self._width:
            self.stream.write("\r" + " " * self._width + "\r")
            self.stream.flush()
            self._width = 0


class _ExitWatch:
    """Fails the run once no client process has been alive for `grace` seconds."""

    def __init__(self, grace: float) -> None:
        self.grace = grace
        self.empty_since: float | None = None

    def check(self, sample: dict[str, Any]) -> None:
        if sample["procs"]:
            self.empty_since = None
            return
        if self.empty_since is None:
            self.empty_since = sample["t"]
        elif sample["t"] - self.empty_since >= self.grace:
            raise BenchError(
                "all client processes exited. Single-instance apps hand off to an already "
                "running copy and quit; otherwise the client crashed or the path is wrong."
            )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _status(label: str, run_name: str, sample: dict[str, Any], duration: float,
            ready: float | None) -> str:
    memory = sample["uss"] if sample["uss"] is not None else sample["rss"]
    text = (
        f"[{label}] {run_name}  {sample['t']:5.1f}/{duration:g}s  cpu {sample['cpu']:6.1f}%  "
        f"mem {fmt_bytes(memory):>8}  procs {sample['procs']}"
    )
    if ready is not None:
        text += f"  usable {ready:.2f}s"
    return text


def _run_summary(run: dict[str, Any]) -> str:
    m = run["metrics"]
    parts = [
        f"idle RAM {fmt_bytes(m.get('idle_memory'))}",
        f"idle CPU {fmt_pct(m.get('idle_cpu'))}",
    ]
    if "settle_time" in m:
        parts.append(f"idle after {fmt_seconds(m.get('settle_time'))}")
    if "ready_time" in m:
        parts.append(f"usable after {fmt_seconds(m.get('ready_time'))}")
    if "js_heap_after_gc" in m:
        parts.append(f"JS heap {fmt_bytes(m.get('js_heap_after_gc'))}")
    return f"run {run['index']}: " + ", ".join(parts)


def _wait_until_gone(names: Sequence[str], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while names and time.monotonic() < deadline:
        if not find_running(names):
            return True
        time.sleep(0.25)
    return not names or not find_running(names)


def _close_existing(spec: LaunchSpec, cfg: RunConfig, progress: Progress) -> None:
    running = find_running(spec.process_names)
    if not running:
        return
    if not cfg.kill_existing:
        raise BenchError(
            f"{spec.name} is already running ({len(running)} processes). Quit it "
            "(including from the system tray) or pass --kill-existing."
        )
    progress.line(f"closing {len(running)} running {spec.name} process(es) (--kill-existing)")
    terminate(running)
    if not _wait_until_gone(spec.process_names, timeout=15):
        raise BenchError(f"{spec.name} is still running after trying to close it")


def _sample_loop(
    tracker: ProcessTracker,
    cfg: RunConfig,
    clock: Callable[[], float],
    on_sample: Callable[[dict[str, Any]], None],
    between: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    # `between` gets called every ~0.1 s while we wait for the next tick, until it returns
    # False. The DevTools probe uses it so the ready time isn't rounded to the interval.
    samples: list[dict[str, Any]] = []
    last = 0.0
    next_tick = cfg.interval
    polling = between is not None
    while True:
        now = clock()
        if now < next_tick:
            wait = next_tick - now
            if polling and between is not None:
                polling = between()
                wait = min(0.1, max(0.0, next_tick - clock()))
            time.sleep(wait)
            continue
        tracker.discover()
        now = clock()  # after discover(): the first scan can take over a second on Windows
        sample = tracker.sample(now, now - last)
        last = now
        samples.append(sample)
        on_sample(sample)
        if now >= cfg.duration:
            return samples
        while next_tick <= now:
            next_tick += cfg.interval


def _launch_once(spec: LaunchSpec, label: str, cfg: RunConfig, index: int,
                 progress: Progress) -> dict[str, Any]:
    _close_existing(spec, cfg, progress)
    run_name = "warm-up" if index == 0 else f"run {index}/{cfg.runs}"
    command = list(spec.command)
    port = (cfg.cdp_port or _free_port()) if cfg.cdp else None
    if port is not None:
        command.append(f"--remote-debugging-port={port}")

    started_at = now_iso()
    wall0 = time.time()
    t0 = time.monotonic()

    def clock() -> float:
        return time.monotonic() - t0

    probe = CdpProbe(port, cfg.cdp_url, cfg.ready_selector, clock) if port is not None else None
    try:
        popen = subprocess.Popen(command, **_popen_kwargs())
    except OSError as err:
        raise BenchError(f"could not start {command[0]}: {err}") from err

    tracker = ProcessTracker(
        root_pid=popen.pid, names=spec.process_names, collect_uss=cfg.collect_uss, since=wall0
    )
    watch = _ExitWatch(cfg.exit_grace)
    notes: list[str] = []
    heap: int | None = None

    def on_sample(sample: dict[str, Any]) -> None:
        ready = probe.ready_time if probe is not None else None
        progress.update(_status(label, run_name, sample, cfg.duration, ready))
        watch.check(sample)

    try:
        samples = _sample_loop(
            tracker, cfg, clock, on_sample, probe.poll_ready if probe is not None else None
        )
        if popen.poll() is not None and samples[-1]["procs"]:
            notes.append(
                "the launched process exited while the client kept running in new processes: "
                "it restarted itself (usually to apply an update) or the executable is a "
                "launcher" + ("; DevTools metrics only cover the first instance" if probe else "")
            )
        if probe is not None:
            heap = probe.heap_after_gc()
    finally:
        progress.clear()
        if probe is not None:
            probe.close()
            notes.extend(probe.notes)
        tracker.terminate_all()
        with contextlib.suppress(subprocess.TimeoutExpired):
            popen.wait(timeout=5)
        if not _wait_until_gone(spec.process_names, timeout=15):
            notes.append("some client processes were still running 15 s after the run")

    metrics = compute_run_metrics(
        samples,
        warmup=cfg.warmup,
        settle_cpu=cfg.settle_cpu,
        settle_window=cfg.settle_window,
        launched=True,
    )
    if probe is not None:
        metrics["ready_time"] = probe.ready_time
        metrics["js_heap_after_gc"] = heap
    return {
        "index": index,
        "started_at": started_at,
        "command": command,
        "metrics": metrics,
        "notes": notes,
        "samples": samples,
    }


def run_launch(spec: LaunchSpec, label: str, cfg: RunConfig, out_dir: Path,
               progress: Progress) -> tuple[Path, dict[str, Any]]:
    """Launches the client cfg.runs times (+1 thrown-away launch with --discard-first)."""
    runs: list[dict[str, Any]] = []
    total = cfg.runs + (1 if cfg.discard_first else 0)

    def result_for(launched: LaunchSpec) -> dict[str, Any]:
        return build_result(
            label=label, mode="launch", target=launched.to_dict(), config=asdict(cfg), runs=runs
        )

    try:
        for attempt in range(total):
            if attempt:
                progress.line(f"cooling down for {cfg.cooldown:g}s")
                time.sleep(cfg.cooldown)
            warm_up = cfg.discard_first and attempt == 0
            spec = refresh_target(spec)  # Discord may have updated itself in the meantime
            run = _launch_once(spec, label, cfg, 0 if warm_up else len(runs) + 1, progress)
            after = refresh_target(spec)
            if after.command != spec.command:
                run["notes"].append(
                    f"{spec.name} installed an update during this run ({spec.build} -> "
                    f"{after.build}), so its numbers include the update. Measure again, or "
                    "use --discard-first."
                )
            spec = after
            if warm_up:
                progress.line("warm-up launch done (not counted)")
                continue
            runs.append(run)
            progress.line(_run_summary(run))
            for note in run["notes"]:
                progress.line(f"  note: {note}")
    except (BenchError, KeyboardInterrupt) as err:
        # don't lose a whole session because the last run went wrong
        if not runs:
            raise
        result = result_for(spec)
        result["incomplete"] = {"requested_runs": cfg.runs, "completed_runs": len(runs)}
        path = save_result(result, out_dir)
        saved = f"the {len(runs)} completed run(s) were saved to {path}"
        if isinstance(err, KeyboardInterrupt):
            print(saved, file=sys.stderr)
            raise
        raise BenchError(f"{err}\n{saved}") from err
    result = result_for(spec)
    return save_result(result, out_dir), result


def run_attach(names: Sequence[str], label: str, cfg: RunConfig, out_dir: Path,
               progress: Progress) -> tuple[Path, dict[str, Any]]:
    """Measures a client that is already running. Never starts or stops anything."""
    if not find_running(names):
        raise BenchError(f"no running process named {', '.join(names)}")
    started_at = now_iso()
    wall0 = time.time()
    t0 = time.monotonic()

    def clock() -> float:
        return time.monotonic() - t0

    tracker = ProcessTracker(names=names, collect_uss=cfg.collect_uss, since=wall0)
    tracker.discover()  # existing processes: only CPU used from now on counts

    def on_sample(sample: dict[str, Any]) -> None:
        progress.update(_status(label, "attach", sample, cfg.duration, None))

    try:
        samples = _sample_loop(tracker, cfg, clock, on_sample)
    finally:
        progress.clear()
    metrics = compute_run_metrics(
        samples, warmup=cfg.warmup, settle_cpu=cfg.settle_cpu,
        settle_window=cfg.settle_window, launched=False,
    )
    run = {
        "index": 1,
        "started_at": started_at,
        "command": [],
        "metrics": metrics,
        "notes": [],
        "samples": samples,
    }
    target = {"id": "attach", "name": ", ".join(names), "command": [], "process_names": list(names)}
    result = build_result(label=label, mode="attach", target=target, config=asdict(cfg), runs=[run])
    return save_result(result, out_dir), result
