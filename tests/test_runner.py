import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client_mod_bench import runner, targets
from client_mod_bench.errors import BenchError
from client_mod_bench.fmt import MIB
from client_mod_bench.results import environment_warnings, group_by_label, load_results
from client_mod_bench.runner import Progress, RunConfig, run_launch
from client_mod_bench.targets import custom_target, refresh_target, resolve_target


def fake_run(index):
    return {
        "index": index,
        "command": ["client"],
        "metrics": {"memory_metric": "uss", "idle_memory": 300 * MIB, "idle_cpu": 1.0},
        "notes": [],
        "samples": [],
    }


class FakeSquirrelInstall:
    """A temporary %LOCALAPPDATA% with a Squirrel-style Discord install, on any OS."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}),
            # only targets.py gets to think it's on Windows. Patching platform.system itself
            # also fooled results.cpu_model() into importing winreg on Linux/macOS CI
            mock.patch.object(targets, "platform", SimpleNamespace(system=lambda: "Windows")),
        ]

    def add_build(self, version):
        build = self.root / "Discord" / f"app-{version}"
        build.mkdir(parents=True)
        (build / "Discord.exe").write_bytes(b"")
        return build

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class RefreshTargetTest(unittest.TestCase):
    def test_follows_discord_self_update(self):
        with FakeSquirrelInstall() as install:
            install.add_build("1.0.9")
            newest = install.add_build("1.0.10")  # numeric order, not alphabetical
            spec = resolve_target("discord", ["--flag"])
            self.assertEqual(spec.build, "app-1.0.10")
            spec.process_names = ["Discord", "Extra"]

            # Discord installs 1.0.11 and deletes the build we started.
            shutil.rmtree(newest)
            install.add_build("1.0.11")
            fresh = refresh_target(spec)
            self.assertEqual(fresh.build, "app-1.0.11")
            self.assertEqual(fresh.command[1:], ["--flag"])
            self.assertEqual(fresh.process_names, ["Discord", "Extra"])

    def test_custom_target_is_unchanged(self):
        spec = custom_target("/opt/app/app", ["-x"])
        self.assertIs(refresh_target(spec), spec)
        self.assertEqual(spec.build, str(Path("/opt/app/app")))


class RunLaunchTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.cfg = RunConfig(runs=3, cooldown=0)
        self.spec = custom_target("client")

    def test_completed_runs_survive_a_failure(self):
        side_effect = [fake_run(1), fake_run(2), BenchError("client vanished")]
        with mock.patch.object(runner, "_launch_once", side_effect=side_effect):
            with self.assertRaises(BenchError) as caught:
                run_launch(self.spec, "partial", self.cfg, self.out, Progress(quiet=True))
        self.assertIn("client vanished", str(caught.exception))
        self.assertIn("2 completed run(s) were saved", str(caught.exception))
        [saved] = list(self.out.glob("*.json"))
        data = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(len(data["runs"]), 2)
        self.assertEqual(data["incomplete"], {"requested_runs": 3, "completed_runs": 2})
        warnings = environment_warnings(group_by_label(load_results([self.out])))
        self.assertTrue(any("stopped early (2 of 3 runs)" in w for w in warnings))

    def test_first_failure_saves_nothing(self):
        with mock.patch.object(runner, "_launch_once", side_effect=BenchError("bad path")):
            with self.assertRaises(BenchError):
                run_launch(self.spec, "broken", self.cfg, self.out, Progress(quiet=True))
        self.assertEqual(list(self.out.glob("*.json")), [])

    def test_notes_an_update_during_a_run(self):
        with FakeSquirrelInstall() as install:
            install.add_build("1.0.1")
            spec = resolve_target("discord")
            launched = []

            def launch(spec, label, cfg, index, progress):
                launched.append(spec.build)
                if index == 1:  # the client updates itself while it is being measured
                    shutil.rmtree(install.root / "Discord" / "app-1.0.1")
                    install.add_build("1.0.2")
                return fake_run(index)

            cfg = RunConfig(runs=2, cooldown=0)
            with mock.patch.object(runner, "_launch_once", side_effect=launch):
                _, result = run_launch(spec, "upd", cfg, self.out, Progress(quiet=True))
        self.assertEqual(launched, ["app-1.0.1", "app-1.0.2"])
        first, second = result["runs"]
        self.assertTrue(any("app-1.0.1 -> app-1.0.2" in note for note in first["notes"]))
        self.assertEqual(second["notes"], [])
        self.assertIn("app-1.0.2", result["target"]["command"][0])


if __name__ == "__main__":
    unittest.main()
