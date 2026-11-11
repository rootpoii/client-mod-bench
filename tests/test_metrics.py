import unittest

from client_mod_bench.fmt import MIB
from client_mod_bench.metrics import (
    compute_run_metrics,
    settle_time,
    startup_cpu_seconds,
    weighted_cpu,
)


def make_samples(cpu_profile, interval=1.0, memory=100 * MIB):
    samples = []
    for i, cpu in enumerate(cpu_profile):
        samples.append({
            "t": round((i + 1) * interval, 6),
            "dt": interval,
            "cpu": cpu,
            "rss": memory * 2,
            "uss": memory + i,
            "procs": 4,
            "by_type": {"browser": 60 * MIB, "renderer": memory - 60 * MIB + i},
        })
    return samples


class SettleTimeTest(unittest.TestCase):
    def test_detects_end_of_startup(self):
        samples = make_samples([200] * 10 + [1] * 20)
        self.assertEqual(settle_time(samples, threshold=5, window=5), 10.0)

    def test_short_dip_is_not_idle(self):
        samples = make_samples([200] * 5 + [1] * 2 + [200] * 5 + [1] * 10)
        self.assertEqual(settle_time(samples, threshold=5, window=5), 12.0)

    def test_never_settles(self):
        self.assertIsNone(settle_time(make_samples([50] * 30), threshold=5, window=5))

    def test_works_with_sub_second_interval(self):
        samples = make_samples([300] * 8 + [0] * 20, interval=0.5)
        self.assertEqual(settle_time(samples, threshold=5, window=2), 4.0)


class CpuTest(unittest.TestCase):
    def test_weighted_by_interval(self):
        samples = [{"t": 1, "dt": 1, "cpu": 10}, {"t": 4, "dt": 3, "cpu": 2}]
        self.assertAlmostEqual(weighted_cpu(samples), 4.0)
        self.assertIsNone(weighted_cpu([]))

    def test_startup_cpu_seconds(self):
        samples = make_samples([200] * 10 + [1] * 20)
        self.assertEqual(startup_cpu_seconds(samples, 10.0), 20.0)


class RunMetricsTest(unittest.TestCase):
    def test_idle_window(self):
        samples = make_samples([200] * 10 + [1] * 20)
        metrics = compute_run_metrics(samples, warmup=15, settle_cpu=5, settle_window=5)
        self.assertEqual(metrics["memory_metric"], "uss")
        self.assertAlmostEqual(metrics["idle_cpu"], 1.0)
        self.assertEqual(metrics["idle_memory"], 100 * MIB + 21.5)
        self.assertEqual(metrics["peak_memory"], 100 * MIB + 29)
        self.assertEqual(metrics["renderer_memory"], 40 * MIB + 21.5)
        self.assertEqual(metrics["processes"], 4)
        self.assertEqual(metrics["settle_time"], 10.0)
        self.assertEqual(metrics["startup_cpu_seconds"], 20.0)

    def test_falls_back_to_rss_when_uss_is_missing(self):
        samples = make_samples([1] * 10)
        samples[3]["uss"] = None
        metrics = compute_run_metrics(samples, warmup=0)
        self.assertEqual(metrics["memory_metric"], "rss")
        self.assertEqual(metrics["idle_memory"], 200 * MIB)

    def test_attach_mode_has_no_startup_metrics(self):
        metrics = compute_run_metrics(make_samples([1] * 10), warmup=0, launched=False)
        self.assertNotIn("settle_time", metrics)
        self.assertNotIn("startup_cpu_seconds", metrics)

    def test_empty_run(self):
        self.assertEqual(compute_run_metrics([], warmup=0), {})


if __name__ == "__main__":
    unittest.main()
