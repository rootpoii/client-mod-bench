"""The client-mod-bench / cmbench command."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .cdp import DEFAULT_READY_SELECTOR
from .compare import build_table, footnote, render_markdown, render_text, table_json
from .errors import CmbError
from .fmt import fmt_bytes, fmt_pct, fmt_seconds
from .report import render_report
from .results import environment_warnings, group_by_label, load_results, order_groups
from .runner import Progress, RunConfig, run_attach, run_launch
from .targets import TARGETS, custom_target, describe_targets, resolve_target


def _number(kind: type, minimum: float, inclusive: bool) -> Any:
    def parse(text: str) -> Any:
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text}") from None
        if value < minimum or (not inclusive and value == minimum):
            raise argparse.ArgumentTypeError(
                f"must be {'at least' if inclusive else 'greater than'} {minimum:g}: {text}"
            )
        return value

    return parse


_positive = _number(float, 0, inclusive=False)
_non_negative = _number(float, 0, inclusive=True)
_count = _number(int, 1, inclusive=True)


def _label_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _add_sampling(parser: argparse.ArgumentParser, duration: float, warmup: float) -> None:
    parser.add_argument("-l", "--label", required=True,
                        help="name of this configuration, e.g. vanilla, vencord, endcord")
    parser.add_argument("-d", "--duration", type=_positive, default=duration,
                        help=f"seconds to measure per run (default: {duration:g})")
    parser.add_argument("-w", "--warmup", type=_non_negative, default=warmup,
                        help="seconds after launch before the idle window starts "
                             f"(default: {warmup:g})")
    parser.add_argument("-i", "--interval", type=_positive, default=1.0,
                        help="seconds between samples (default: 1)")
    parser.add_argument("--no-uss", action="store_true",
                        help="skip unique set size; report resident memory (RSS) only")
    parser.add_argument("-o", "--out", default="results",
                        help="directory for result files (default: ./results)")
    parser.add_argument("-q", "--quiet", action="store_true", help="no live progress")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client-mod-bench",
        description="Reproducible RAM, CPU and startup benchmarks for Discord client mods "
                    "and Electron chat clients.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = sub.add_parser(
        "run", help="launch a client several times and measure it",
        description="Launch the client, sample all of its processes, close it, repeat.",
        epilog="Arguments after a bare -- are passed to the client unchanged, e.g. "
               "client-mod-bench run -l test --exe ./App -- --disable-gpu",
    )
    _add_sampling(run, duration=90, warmup=30)
    target = run.add_mutually_exclusive_group()
    target.add_argument("-t", "--target", choices=list(TARGETS),
                        help="built-in target (default: discord)")
    target.add_argument("--exe", help="launch this executable instead of a built-in target")
    run.add_argument("-a", "--arg", dest="args", action="append", default=[], metavar="ARG",
                     help="extra argument for the client (repeatable); write --arg=--flag "
                          "for values that start with a dash, or put them after --")
    run.add_argument("-p", "--process-name", action="append", metavar="NAME",
                     help="also track processes with this name (repeatable)")
    run.add_argument("-n", "--runs", type=_count, default=3, help="measured runs (default: 3)")
    run.add_argument("--discard-first", action="store_true",
                     help="do one extra launch first and throw it away (lets updates and "
                          "caches settle)")
    run.add_argument("--cooldown", type=_non_negative, default=10.0,
                     help="seconds to wait between runs (default: 10)")
    run.add_argument("--settle-cpu", type=_positive, default=5.0,
                     help="CPU %% of one core under which the client counts as idle "
                          "(default: 5)")
    run.add_argument("--settle-window", type=_positive, default=5.0,
                     help="seconds the CPU must stay under --settle-cpu (default: 5)")
    run.add_argument("--cdp", action="store_true",
                     help="also measure launch-to-usable time and JS heap after GC through "
                          "the Chrome DevTools Protocol")
    run.add_argument("--cdp-port", type=int, default=0,
                     help="remote debugging port (default: a free port)")
    run.add_argument("--cdp-url", default="discord.com",
                     help="substring of the main window URL (default: discord.com)")
    run.add_argument("--ready-selector", default=DEFAULT_READY_SELECTOR,
                     help="CSS selector that marks the client as usable "
                          f"(default: {DEFAULT_READY_SELECTOR})")
    run.add_argument("--kill-existing", action="store_true",
                     help="close running instances of the target before each run")

    attach = sub.add_parser(
        "attach", help="measure a client that is already running",
        description="Sample running processes by name without launching or closing them.",
    )
    _add_sampling(attach, duration=60, warmup=0)
    attach.add_argument("-p", "--process-name", action="append", required=True, metavar="NAME",
                        help="process name to measure, e.g. Discord (repeatable)")

    compare = sub.add_parser("compare", help="print a comparison table")
    compare.add_argument("paths", nargs="+", help="result files or directories")
    compare.add_argument("-b", "--baseline", help="label to compute relative differences against")
    compare.add_argument("-f", "--format", choices=["markdown", "text", "json"],
                         default="markdown", help="output format (default: markdown)")
    compare.add_argument("--spread", action="store_true", help="add ± standard deviation")
    compare.add_argument("--order", type=_label_list, help="comma-separated column order")
    compare.add_argument("--no-footnote", action="store_true",
                         help="leave out the methodology line under the table")

    report = sub.add_parser("report", help="write a self-contained HTML report")
    report.add_argument("paths", nargs="+", help="result files or directories")
    report.add_argument("-o", "--output", default="report.html",
                        help="output file (default: report.html)")
    report.add_argument("-b", "--baseline", help="label to compute relative differences against")
    report.add_argument("--title", default="Client benchmark", help="report title")
    report.add_argument("--order", type=_label_list, help="comma-separated client order")

    sub.add_parser("targets", help="show which built-in targets are installed")
    return parser


def _config(args: argparse.Namespace, launched: bool) -> RunConfig:
    cfg = RunConfig(
        duration=args.duration,
        warmup=args.warmup,
        interval=args.interval,
        collect_uss=not args.no_uss,
    )
    if launched:
        cfg.runs = args.runs
        cfg.discard_first = args.discard_first
        cfg.cooldown = args.cooldown
        cfg.settle_cpu = args.settle_cpu
        cfg.settle_window = args.settle_window
        cfg.cdp = args.cdp
        cfg.cdp_port = args.cdp_port
        cfg.cdp_url = args.cdp_url
        cfg.ready_selector = args.ready_selector
        cfg.kill_existing = args.kill_existing
    else:
        cfg.runs = 1
        cfg.cooldown = 0.0
    if cfg.warmup >= cfg.duration:
        raise CmbError("--warmup must be shorter than --duration")
    if cfg.interval > cfg.duration:
        raise CmbError("--interval must not be longer than --duration")
    return cfg


def _print_result(result: dict[str, Any], path: Path, notes: bool = True) -> None:
    summary = result["summary"]

    def med(key: str) -> float | None:
        return summary[key]["median"] if key in summary else None

    parts = [f"idle RAM {fmt_bytes(med('idle_memory'))}", f"idle CPU {fmt_pct(med('idle_cpu'))}"]
    if "settle_time" in summary:
        parts.append(f"idle after {fmt_seconds(med('settle_time'))}")
    if "ready_time" in summary:
        parts.append(f"usable after {fmt_seconds(med('ready_time'))}")
    if "js_heap_after_gc" in summary:
        parts.append(f"JS heap {fmt_bytes(med('js_heap_after_gc'))}")
    runs = len(result["runs"])
    print(f"{result['label']} (median of {runs} run{'s' if runs != 1 else ''}): "
          + ", ".join(parts))
    print(f"saved {path}")
    if notes:
        for note in dict.fromkeys(n for run in result["runs"] for n in run.get("notes", [])):
            print(f"note: {note}", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _config(args, launched=True)
    client_args = [*args.args, *args.passthrough]
    if args.exe:
        spec = custom_target(args.exe, client_args, args.process_name or [])
    else:
        spec = resolve_target(args.target or "discord", client_args)
        if args.process_name:
            spec.process_names = list(args.process_name)
    progress = Progress(quiet=args.quiet)
    progress.line(f"{args.label}: {' '.join(spec.command)}")
    progress.line(f"{cfg.runs} run(s) of {cfg.duration:g}s, idle window starts at "
                  f"{cfg.warmup:g}s")
    path, result = run_launch(spec, args.label, cfg, Path(args.out), progress)
    _print_result(result, path, notes=args.quiet)  # otherwise already shown after each run
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    cfg = _config(args, launched=False)
    progress = Progress(quiet=args.quiet)
    path, result = run_attach(args.process_name, args.label, cfg, Path(args.out), progress)
    _print_result(result, path)
    return 0


def _groups(paths: Sequence[str], order: Sequence[str] | None,
            baseline: str | None) -> list[Any]:
    groups = order_groups(group_by_label(load_results(paths)), order, baseline)
    for warning in environment_warnings(groups):
        print(f"warning: {warning}", file=sys.stderr)
    return groups


def cmd_compare(args: argparse.Namespace) -> int:
    groups = _groups(args.paths, args.order, args.baseline)
    if args.format == "json":
        print(json.dumps(table_json(groups), indent=2))
        return 0
    table = build_table(groups, baseline=args.baseline, spread=args.spread)
    print(render_markdown(table) if args.format == "markdown" else render_text(table))
    if not args.no_footnote:
        print()
        print(footnote(groups))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    groups = _groups(args.paths, args.order, args.baseline)
    html = render_report(groups, baseline=args.baseline, title=args.title)
    output = Path(args.output)
    output.write_text(html, encoding="utf-8")
    print(f"wrote {output}")
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    rows = describe_targets()
    width = max(len(target_id) for target_id, _, _ in rows)
    for target_id, name, spec in rows:
        where = " ".join(spec.command) if spec else "not found"
        print(f"{target_id.ljust(width)}  {name.ljust(15)}  {where}")
    return 0


COMMANDS = {
    "run": cmd_run,
    "attach": cmd_attach,
    "compare": cmd_compare,
    "report": cmd_report,
    "targets": cmd_targets,
}


def _tolerant_streams() -> None:
    # on Windows a redirected stdout uses the ANSI code page, which can't encode ± or –
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _tolerant_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, passthrough = argv[:split], argv[split + 1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if passthrough and args.command != "run":
        parser.error("arguments after -- are only used by the run command")
    args.passthrough = passthrough
    try:
        return COMMANDS[args.command](args)
    except CmbError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; the client was closed", file=sys.stderr)
        return 130
