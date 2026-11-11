"""Where Discord (Stable/PTB/Canary) and Vesktop live on each OS."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import TargetError

Resolved = tuple[list[str], list[str]]  # (command, process names)


@dataclass
class LaunchSpec:
    id: str
    name: str
    command: list[str]
    process_names: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)  # extra client arguments (end of command)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def build(self) -> str:
        # e.g. "app-1.0.9267" for Discord on Windows, otherwise the full path
        exe = Path(self.command[0])
        return exe.parent.name if exe.parent.name.startswith("app-") else str(exe)


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", path.name))


def _squirrel_exe(folder: str, exe: str) -> Path | None:
    # Discord on Windows is a Squirrel install: %LOCALAPPDATA%\Discord\app-1.0.xxxx\.
    # Starting the newest build directly instead of going through Update.exe keeps the
    # updater out of the numbers, and the process we start is the real main process.
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    builds = [p / exe for p in (Path(local) / folder).glob("app-*") if (p / exe).is_file()]
    if not builds:
        return None
    return max(builds, key=lambda p: _version_key(p.parent))


def _first_file(paths: Sequence[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None


def _flatpak(app_id: str) -> list[str] | None:
    if not shutil.which("flatpak"):
        return None
    try:
        info = subprocess.run(
            ["flatpak", "info", app_id], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return ["flatpak", "run", app_id] if info.returncode == 0 else None


def _mac_app(app: str) -> Path | None:
    roots = (Path("/Applications"), Path.home() / "Applications")
    return _first_file([root / f"{app}.app" / "Contents" / "MacOS" / app for root in roots])


@dataclass(frozen=True)
class DiscordBuild:
    binary: str  # Windows install folder + exe name, Linux binary and process name
    mac_app: str
    linux_dir: str
    flatpak_id: str | None


def _discord(build: DiscordBuild) -> Resolved | None:
    system = platform.system()
    if system == "Windows":
        exe = _squirrel_exe(build.binary, f"{build.binary}.exe")
        return ([str(exe)], [build.binary]) if exe else None
    # TODO: the macOS and Linux paths below haven't been tried on a real machine yet
    if system == "Darwin":
        exe = _mac_app(build.mac_app)
        return ([str(exe)], [build.mac_app]) if exe else None
    exe = _first_file([
        Path("/usr/share") / build.linux_dir / build.binary,
        Path("/opt") / build.linux_dir / build.binary,
        Path("/opt") / build.binary / build.binary,
    ]) or _which(build.linux_dir)
    if exe:
        return [str(exe)], [build.binary]
    command = _flatpak(build.flatpak_id) if build.flatpak_id else None
    return (command, [build.binary]) if command else None


def _vesktop() -> Resolved | None:
    system = platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        exe = _first_file([Path(local) / "Programs" / "vesktop" / "Vesktop.exe"]) if local else None
        return ([str(exe)], ["Vesktop"]) if exe else None
    if system == "Darwin":
        exe = _mac_app("Vesktop")
        return ([str(exe)], ["Vesktop"]) if exe else None
    exe = _which("vesktop") or _first_file([Path("/opt/Vesktop/vesktop")])
    if exe:
        return [str(exe)], ["vesktop"]
    command = _flatpak("dev.vencord.Vesktop")
    return (command, ["vesktop"]) if command else None


@dataclass(frozen=True)
class BuiltinTarget:
    id: str
    name: str
    resolve: Callable[[], Resolved | None]


_STABLE = DiscordBuild("Discord", "Discord", "discord", "com.discordapp.Discord")
_PTB = DiscordBuild("DiscordPTB", "Discord PTB", "discord-ptb", None)
_CANARY = DiscordBuild("DiscordCanary", "Discord Canary", "discord-canary", None)

TARGETS: dict[str, BuiltinTarget] = {
    target.id: target
    for target in (
        BuiltinTarget("discord", "Discord Stable", lambda: _discord(_STABLE)),
        BuiltinTarget("discord-ptb", "Discord PTB", lambda: _discord(_PTB)),
        BuiltinTarget("discord-canary", "Discord Canary", lambda: _discord(_CANARY)),
        BuiltinTarget("vesktop", "Vesktop", _vesktop),
    )
}


def resolve_target(target_id: str, extra_args: Sequence[str] = ()) -> LaunchSpec:
    target = TARGETS.get(target_id)
    if target is None:
        raise TargetError(f"unknown target {target_id!r}; choose one of: {', '.join(TARGETS)}")
    resolved = target.resolve()
    if resolved is None:
        raise TargetError(
            f"{target.name} was not found on this machine. "
            "Pass --exe with the path to its executable instead."
        )
    command, names = resolved
    return LaunchSpec(
        target.id, target.name, [*command, *extra_args], list(names), list(extra_args)
    )


def refresh_target(spec: LaunchSpec) -> LaunchSpec:
    # Discord updates itself on launch and deletes the old app-* folder, so the path from
    # run 1 can be gone by run 2 (happened with Canary 1.0.1319 -> 1.0.1320).
    if spec.id not in TARGETS:
        return spec
    try:
        fresh = resolve_target(spec.id, spec.args)
    except TargetError:
        return spec
    fresh.process_names = list(spec.process_names)  # keep --process-name overrides
    return fresh


def custom_target(
    exe: str, extra_args: Sequence[str] = (), process_names: Sequence[str] = ()
) -> LaunchSpec:
    # without process_names only the launched process and its children are measured
    return LaunchSpec(
        "custom", Path(exe).stem or exe, [exe, *extra_args], list(process_names), list(extra_args)
    )


def describe_targets() -> list[tuple[str, str, LaunchSpec | None]]:
    rows: list[tuple[str, str, LaunchSpec | None]] = []
    for target in TARGETS.values():
        try:
            spec: LaunchSpec | None = resolve_target(target.id)
        except TargetError:
            spec = None
        rows.append((target.id, target.name, spec))
    return rows
