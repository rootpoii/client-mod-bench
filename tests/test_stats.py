import unittest

from client_mod_bench.stats import mean, median, stdev, summarize


class StatsTest(unittest.TestCase):
    def test_median_odd_and_even(self):
        self.assertEqual(median([3, 1, 2]), 2)
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_missing_values_are_skipped(self):
        self.assertEqual(median([None, 5, None]), 5)
        self.assertIsNone(median([None]))
        self.assertIsNone(mean([]))

    def test_mean_and_sample_stdev(self):
        self.assertAlmostEqual(mean([1, 2, 3, 4]), 2.5)
        self.assertAlmostEqual(stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.13808994, places=6)
        self.assertEqual(stdev([7]), 0.0)

    def test_summarize(self):
        summary = summarize([1, 2, 3])
        self.assertEqual(summary["n"], 3)
        self.assertEqual((summary["min"], summary["median"], summary["max"]), (1, 2, 3))
        self.assertIsNone(summarize([None]))


if __name__ == "__main__":
    unittest.main()
