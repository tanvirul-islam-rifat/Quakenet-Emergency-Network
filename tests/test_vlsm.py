import unittest

from tools.validate_design import ROOT, read_csv
from tools.vlsm import allocate, load_requirements, prefix_for_hosts


class VlsmTests(unittest.TestCase):
    def test_prefix_selection_matches_the_plan(self):
        expected = {5000: 19, 4094: 20, 4095: 19, 1040: 21, 1022: 22, 1023: 21, 500: 23, 320: 23, 180: 24,
                    100: 25, 50: 26, 4: 29, 2: 30}
        for hosts, prefix in expected.items():
            self.assertEqual(prefix_for_hosts(hosts), prefix, hosts)

    def test_allocation_reproduces_the_published_plan(self):
        plan = allocate("18.50.0.0/16", load_requirements(ROOT / "addressing" / "vlsm_requirements.csv"))
        published = read_csv(ROOT / "addressing" / "vlsm_plan.csv")
        self.assertEqual([str(p["subnet"]) for p in plan], [r["subnet"] for r in published])
        self.assertEqual(str(plan[-1]["broadcast"]), "18.50.45.223")

    def test_blocks_are_aligned_and_do_not_overlap(self):
        plan = allocate("10.0.0.0/16", [("a", 5000), ("b", 30), ("c", 2), ("d", 2), ("e", 1040)])
        nets = [p["subnet"] for p in plan]
        for n in nets:
            self.assertEqual(int(n.network_address) % n.num_addresses, 0)
        for i, a in enumerate(nets):
            for b in nets[i + 1:]:
                self.assertFalse(a.overlaps(b))

    def test_exhaustion_is_reported(self):
        with self.assertRaises(ValueError):
            allocate("10.0.0.0/24", [("big", 200), ("also big", 100)])


if __name__ == "__main__":
    unittest.main()
