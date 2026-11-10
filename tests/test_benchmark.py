import unittest

from scripts.benchmark import descendants, parse_footprint


class BenchmarkTests(unittest.TestCase):
    def test_footprint_units_are_normalised_to_kilobytes(self):
        self.assertEqual(parse_footprint("    phys_footprint: 5936 KB\n"), 5936)
        self.assertEqual(parse_footprint("phys_footprint: 14 MB"), 14 * 1024)
        self.assertEqual(parse_footprint("phys_footprint: 1.5 GB"), 1.5 * 1024 * 1024)
        self.assertEqual(parse_footprint("no such process"), 0)

    def test_descendants_include_detached_daemons_started_by_a_session(self):
        # session 10 started daemon 20 (still its child) which runs backend 30
        table = {10: 1, 11: 1, 20: 10, 30: 20, 31: 30, 99: 1}

        self.assertEqual(descendants(table, [10, 11]), {10, 11, 20, 30, 31})


if __name__ == "__main__":
    unittest.main()
