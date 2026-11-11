"""End to end: launch a real process tree, measure it, close it, compare, report."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import psutil

from client_mod_bench.cli import main
from client_mod_bench.fmt import MIB
from client_mod_bench.runner import Progress, RunConfig, run_launch
from client_mod_bench.targets import custom_target

FIXTURE = Path(__file__).parent / "fixtures" / "busy_tree.py"


def fixture_processes():
    found = []
    for proc in psutil.process_iter(["cmdline"]):
        if any(FIXTURE.name in part for part in proc.info.get("cmdline") or []):
            found.append(proc)
    return found


class LaunchTest(unittest.TestCase):
    def test_measures_whole_tree_and_cleans_up(self):
        spec = custom_target(sys.executable, [str(FIXTURE)])
        cfg = RunConfig(duration=7, warmup=4, interval=0.5, runs=1, cooldown=0,
                        settle_cpu=25, settle_window=1.0)
        with tempfile.TemporaryDirectory() as tmp:
            path, result = run_launch(spec, "fixture", cfg, Path(tmp), Progress(quiet=True))
            self.assertTrue(path.is_file())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["label"], "fixture")
            metrics = result["runs"][0]["metrics"]
            self.assertGreaterEqual(metrics["processes"], 2)
            self.assertGreater(metrics["idle_memory"], 20 * MIB)
            self.assertLess(metrics["idle_cpu"], 25)
            self.assertIsNotNone(metrics["settle_time"])
            self.assertGreater(metrics["settle_time"], 0.9)
            self.assertLess(metrics["settle_time"], 4.0)
            self.assertGreater(metrics["startup_cpu_seconds"], 1.0)
            self.assertEqual(fixture_processes(), [])

            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["compare", tmp, "--format", "text"]), 0)
                report = Path(tmp) / "report.html"
                self.assertEqual(main(["report", tmp, "-o", str(report)]), 0)
            self.assertIn("fixture", out.getvalue())
            self.assertIn("<svg", report.read_text(encoding="utf-8"))

    def test_notes_a_client_that_restarts_itself(self):
        spec = custom_target(sys.executable, [str(FIXTURE), "relaunch"])
        cfg = RunConfig(duration=3, warmup=1.5, interval=0.5, runs=1, cooldown=0)
        with tempfile.TemporaryDirectory() as tmp:
            _, result = run_launch(spec, "relaunch", cfg, Path(tmp), Progress(quiet=True))
        run = result["runs"][0]
        self.assertGreaterEqual(run["metrics"]["processes"], 1)
        self.assertTrue(any("restarted itself" in note for note in run["notes"]), run["notes"])
        self.assertEqual(fixture_processes(), [])


class CliTest(unittest.TestCase):
    def test_bad_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run", "-l", "x", "--exe", "x", "-d", "5", "-w", "9"]), 1)
            self.assertEqual(main(["compare", "does-not-exist"]), 1)
            # arguments after -- go to the client instead of being parsed as options
            argv = ["run", "-l", "x", "--exe", "x", "-d", "5", "-w", "9", "--", "--headless"]
            self.assertEqual(main(argv), 1)
            with self.assertRaises(SystemExit):
                main(["compare", "results", "--", "--x"])

    def test_targets_lists_builtins(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["targets"]), 0)
        self.assertIn("discord-canary", out.getvalue())


if __name__ == "__main__":
    unittest.main()
