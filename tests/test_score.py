"""Tests for scoreboard match semantics and metric math."""

import unittest

from openedge_agent.score import aggregate, score_case, target_matches


class TestMatchSemantics(unittest.TestCase):
    def test_schema_exact_only(self):
        self.assertTrue(target_matches("schema:Order", "schema:Order"))
        self.assertFalse(target_matches("schema:Order", "schema:OrderLine"))
        self.assertFalse(target_matches("schema:Order", "corpus/oe/oe-entry.p#main"))

    def test_unit_exact_only(self):
        g = "corpus/oe/oe-credit.p#check-credit"
        self.assertTrue(target_matches(g, g))
        self.assertFalse(target_matches(g, "corpus/oe/oe-credit.p#main"))

    def test_file_level_matches_any_chunk(self):
        g = "corpus/oe/oe-ship.p"
        self.assertTrue(target_matches(g, "corpus/oe/oe-ship.p#main"))
        self.assertTrue(target_matches(g, "corpus/oe/oe-ship.p#ship-line"))
        self.assertTrue(target_matches(g, "corpus/oe/oe-ship.p"))
        self.assertFalse(target_matches(g, "corpus/oe/oe-ship.pretend#x"))


class TestMetrics(unittest.TestCase):
    def _fake(self):
        case = {
            "id": "T1",
            "category": "flow",
            "question": "q",
            "gold": ["corpus/a.p", "corpus/b.p#f"],
            "acceptable": ["schema:X"],
        }
        hits = [
            {"id": "corpus/a.p#main"},   # gold 1 at rank 1
            {"id": "schema:X"},          # acceptable at rank 2
            {"id": "corpus/z.p#main"},   # noise
            {"id": "corpus/b.p#f"},      # gold 2 at rank 4
            {"id": "corpus/y.p#main"},   # noise
            {"id": "corpus/w.p#main"},
        ]
        return case, hits

    def test_score_case(self):
        case, hits = self._fake()
        d = score_case(case, hits)
        self.assertEqual(d["gold_ranks"]["corpus/a.p"], 1)
        self.assertEqual(d["gold_ranks"]["corpus/b.p#f"], 4)
        self.assertEqual(d["first_gold_rank"], 1)
        self.assertAlmostEqual(d["precision_at_5"], 3 / 5)  # 2 gold + 1 acceptable

    def test_aggregate(self):
        case, hits = self._fake()
        d = score_case(case, hits)
        agg = aggregate([d], [case])
        m = agg["metrics"]
        self.assertAlmostEqual(m["micro_recall@1"], 0.5)
        self.assertAlmostEqual(m["micro_recall@5"], 1.0)
        self.assertAlmostEqual(m["strict_case_recall@5"], 1.0)
        self.assertAlmostEqual(m["mrr"], 1.0)


if __name__ == "__main__":
    unittest.main()
