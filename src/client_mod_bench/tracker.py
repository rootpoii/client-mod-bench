"""Finding a client's processes and sampling their CPU and memory.

Discord is about six processes (main, renderer, gpu, a couple of utility ones, crashpad)
and it can relaunch itself through the updater, so we follow both the tree of the process
we started and anything whose name matches.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psutil

# create_time() and time.time() don't agree to the millisecond
_CLOCK_SLACK = 0.05


def normalize_name(name: str) -> str:
    name = name.strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower()


def matches_name(process_name: str, names: Iterable[str]) -> bool:
    # case-insensitive, ignores .exe, and also catches macOS helpers like
    # "Discord Helper (GPU)"
    candidate = normalize_name(process_name)
    for name in names:
        wanted = normalize_name(name)
        if candidate == wanted or candidate.startswith(wanted + " helper"):
            return True
    return False


def classify_process(name: str, cmdline: Sequence[str]) -> str:
    # "browser" is what Chromium calls the main process
    if "crashpad" in name.lower():
        return "crashpad"
    for arg in cmdline[1:]:
        if arg.startswith("--type="):
            kind = arg.partition("=")[2]
            return {"gpu-process": "gpu", "crashpad-handler": "crashpad"}.get(kind, kind or "other")
    return "browser"


def _cpu_seconds(times: Any) -> float:
    return float(times.user + times.system)


@dataclass
class _Tracked:
    proc: psutil.Process
    kind: str
    ppid: int
    cpu: float  # cumulative CPU seconds at the previous sample


class ProcessTracker:
    def __init__(
        self,
        *,
        root_pid: int | None = None,
        names: Sequence[str] = (),
        exclude: Iterable[int] = (),
        collect_uss: bool = True,
        since: float | None = None,
    ) -> None:
        self.names = list(names)
        self.exclude = set(exclude)
        self.collect_uss = collect_uss
        self.ever_seen = False
        self._tracked: dict[int, _Tracked] = {}
        self._uss_denied: set[int] = set()
        self._last_scan = time.time() if since is None else since
        if root_pid is not None:
            with contextlib.suppress(psutil.Error):
                self._add(psutil.Process(root_pid), self._last_scan)

    def __len__(self) -> int:
        return len(self._tracked)

    def _add(self, proc: psutil.Process, born_after: float) -> bool:
        if proc.pid in self._tracked or proc.pid in self.exclude:
            return False
        try:
            with proc.oneshot():
                created = proc.create_time()
                cpu = _cpu_seconds(proc.cpu_times())
                name = proc.name()
                ppid = proc.ppid()
                try:
                    cmdline = proc.cmdline()
                except psutil.Error:
                    cmdline = []
        except psutil.Error:
            return False
        # A process born after the previous scan did all of its work inside the current
        # interval. One that already existed (attach mode) only counts from now on.
        baseline = 0.0 if created >= born_after - _CLOCK_SLACK else cpu
        self._tracked[proc.pid] = _Tracked(proc, classify_process(name, cmdline), ppid, baseline)
        self.ever_seen = True
        return True

    def discover(self) -> None:
        born_after = self._last_scan
        self._last_scan = time.time()
        if self.names:
            for proc in psutil.process_iter(["name"]):
                if proc.pid not in self._tracked and matches_name(
                    proc.info.get("name") or "", self.names
                ):
                    self._add(proc, born_after)
        # children() takes a full process snapshot on every call, so only walk from the
        # top of each tree (normally that's just the process we launched)
        tops = [t.proc for t in self._tracked.values() if t.ppid not in self._tracked]
        for top in tops:
            try:
                descendants = top.children(recursive=True)
            except psutil.Error:
                continue
            for child in descendants:
                self._add(child, born_after)

    def _uss(self, proc: psutil.Process) -> int | None:
        if not self.collect_uss or proc.pid in self._uss_denied:
            return None
        try:
            return int(proc.memory_full_info().uss)
        except psutil.AccessDenied:
            self._uss_denied.add(proc.pid)
            return None

    def sample(self, t: float, dt: float) -> dict[str, Any]:
        cpu_used = 0.0
        rss = uss = count = 0
        uss_complete = self.collect_uss
        by_type: dict[str, int] = {}
        for pid, tracked in list(self._tracked.items()):
            proc = tracked.proc
            try:
                with proc.oneshot():
                    cpu = _cpu_seconds(proc.cpu_times())
                    resident = int(proc.memory_info().rss)
                    unique = self._uss(proc)
            except psutil.NoSuchProcess:
                del self._tracked[pid]
                continue
            except psutil.AccessDenied:
                continue
            cpu_used += max(0.0, cpu - tracked.cpu)
            tracked.cpu = cpu
            count += 1
            rss += resident
            if unique is None:
                uss_complete = False
            else:
                uss += unique
            memory = unique if unique is not None else resident
            by_type[tracked.kind] = by_type.get(tracked.kind, 0) + memory
        return {
            "t": round(t, 3),
            "dt": round(dt, 3),
            "cpu": round(cpu_used / dt * 100.0, 3) if dt > 0 else 0.0,
            "rss": rss,
            "uss": uss if uss_complete and count else None,
            "procs": count,
            "by_type": by_type,
        }

    def processes(self) -> list[psutil.Process]:
        return [tracked.proc for tracked in self._tracked.values()]

    def terminate_all(self) -> None:
        with contextlib.suppress(psutil.Error):
            self.discover()
        terminate(self.processes())
        self._tracked.clear()


def find_running(names: Sequence[str]) -> list[psutil.Process]:
    if not names:
        return []
    return [
        proc
        for proc in psutil.process_iter(["name"])
        if matches_name(proc.info.get("name") or "", names)
    ]


def terminate(procs: Iterable[psutil.Process], timeout: float = 10.0) -> None:
    procs = list(procs)
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    if alive:
        psutil.wait_procs(alive, timeout=5)
