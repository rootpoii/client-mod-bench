import unittest

from client_mod_bench.compare import build_table, footnote, render_markdown, render_text
from client_mod_bench.errors import ResultError
from client_mod_bench.fmt import MIB
from client_mod_bench.report import render_report
from client_mod_bench.results import (
    environment_warnings,
    group_by_label,
    order_groups,
    summarize_runs,
)


def fake_result(label, memory, cpu, runs=3, cpu_name="Test CPU 9000"):
    run_list = []
    for i in range(runs):
        samples = [
            {"t": float(t), "dt": 1.0, "cpu": cpu * (20 if t < 5 else 1), "rss": memory * 2,
             "uss": memory + i * MIB, "procs": 6, "by_type": {"renderer": memory // 2}}
            for t in range(1, 41)
        ]
        metrics = {
            "memory_metric": "uss",
            "idle_memory": memory + i * MIB,
            "idle_cpu": cpu,
            "processes": 6.0,
            "settle_time": 4.0 + i * 0.1,
        }
        run_list.append({"index": i + 1, "metrics": metrics, "notes": [], "samples": samples})
    return {
        "schema": 1,
        "label": label,
        "mode": "launch",
        "config": {"duration": 40, "warmup": 10, "interval": 1.0},
        "system": {"cpu": cpu_name, "os": "Windows", "os_release": "10",
                   "memory_total": 32 * 1024 ** 3, "logical_cpus": 12, "physical_cpus": 6},
        "runs": run_list,
        "summary": summarize_runs(run_list),
    }


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.groups = group_by_label([
            fake_result("vanilla", 400 * MIB, 1.5),
            fake_result("endcord", 440 * MIB, 1.8),
        ])

    def test_same_label_is_merged(self):
        groups = group_by_label([fake_result("a", MIB, 1), fake_result("a", MIB, 1, runs=2)])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].runs), 5)

    def test_table_with_baseline(self):
        table = build_table(self.groups, baseline="vanilla")
        self.assertEqual(table.header, ["Metric", "vanilla (baseline)", "endcord"])
        rows = {row[0]: row[1:] for row in table.rows}
        self.assertEqual(rows["Idle RAM"], ["401 MB", "441 MB (+10.0%)"])
        self.assertEqual(rows["Idle CPU (one core)"], ["1.50%", "1.80% (+20.0%)"])
        self.assertEqual(rows["Runs"], ["3", "3"])
        self.assertNotIn("JS heap after GC", rows)

    def test_spread(self):
        rows = {row[0]: row[1:] for row in build_table(self.groups, spread=True).rows}
        self.assertEqual(rows["Idle RAM"][0], "401 MB ±1.0 MB")

    def test_unknown_baseline(self):
        with self.assertRaises(ResultError):
            build_table(self.groups, baseline="nope")

    def test_renderers(self):
        table = build_table(self.groups, baseline="vanilla")
        markdown = render_markdown(table)
        self.assertTrue(markdown.startswith("| Metric | vanilla (baseline) | endcord |\n"))
        self.assertIn("| :--- | ---: | ---: |", markdown)
        text = render_text(table)
        self.assertIn("Idle RAM", text)
        self.assertIn("Median of 3 runs", footnote(self.groups))
        self.assertIn("Test CPU 9000", footnote(self.groups))

    def test_order(self):
        ordered = order_groups(self.groups, ["endcord"])
        self.assertEqual([g.label for g in ordered], ["endcord", "vanilla"])
        by_baseline = order_groups(list(reversed(self.groups)), None, baseline="vanilla")
        self.assertEqual([g.label for g in by_baseline], ["vanilla", "endcord"])
        with self.assertRaises(ResultError):
            order_groups(self.groups, ["missing"])

    def test_environment_warnings(self):
        self.assertEqual(environment_warnings(self.groups), [])
        mixed = group_by_label([fake_result("a", MIB, 1), fake_result("b", MIB, 1, cpu_name="X")])
        self.assertTrue(any("different machines" in w for w in environment_warnings(mixed)))


class ReportTest(unittest.TestCase):
    def test_report_is_self_contained(self):
        groups = group_by_label([fake_result("vanilla", 400 * MIB, 1.5),
                                 fake_result("endcord <b>", 440 * MIB, 1.8)])
        html = render_report(groups, baseline="vanilla")
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertEqual(html.count("<svg"), 2)
        self.assertEqual(html.count("<svg"), html.count("</svg>"))
        self.assertIn("endcord &lt;b&gt;", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("http", html.replace("http-equiv", ""))


if __name__ == "__main__":
    unittest.main()
