#!/usr/bin/env python3
"""Checks for evidence-group scoring, source arithmetic and bounded graph selection."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_epl_retrieval as benchmark


class EPLRetrievalTests(unittest.TestCase):
    def test_equivalent_sources_satisfy_one_fact(self):
        groups = [{"candidates": {"season-table": ["value"], "club-season": ["value"]}}]
        actual = benchmark.evidence_score(["club-season"], groups)
        self.assertEqual(actual["complete_evidence@1"], 1)
        self.assertEqual(actual["evidence_coverage@5"], 1)

    def test_comparison_needs_both_facts(self):
        groups = [{"candidates": {"a": ["A"]}}, {"candidates": {"b": ["B"]}}]
        actual = benchmark.evidence_score(["noise", "a"], groups)
        self.assertEqual(actual["hit@5"], 1)
        self.assertEqual(actual["complete_evidence@5"], 0)
        self.assertEqual(actual["evidence_coverage@5"], .5)
        self.assertEqual(actual["mrr@10"], .5)
        self.assertEqual(benchmark.evidence_score(["a", "b"], groups)["complete_evidence@5"], 1)

    def test_duplicate_results_are_rejected_and_unknowns_separate(self):
        with self.assertRaises(ValueError):
            benchmark.evidence_score(["a", "a"], [{"candidates": {"a": ["A"]}}])
        self.assertIsNone(benchmark.evidence_score(["some-page"], []))
        rows = [{"metrics": benchmark.evidence_score(["a"], [{"candidates": {"a": ["A"]}}]), "ranked": ["a"], "error": None},
                {"metrics": None, "ranked": ["noise"], "error": None}]
        summary = benchmark.quality_summary(rows)
        self.assertEqual(summary["metrics"]["complete_evidence@5"], 1)
        self.assertEqual(summary["unknown_nonempty_rate"], 1)

    def test_reciprocal_rank_fusion_prefers_repeated_result(self):
        self.assertEqual(benchmark.fused_rank([["a", "b"], ["b", "c"]]), ["b", "a", "c"])

    def test_links_preserve_three_seeds_and_exclude_unresolved_targets(self):
        class FakeApi:
            def __init__(self):
                self.paths = []

            def get(self, path, params):
                self.paths.append(path)
                if path == "/v1/search":
                    return {"results": [{"id": f"seed{i}"} for i in range(10)]}
                return {"outgoing": [{"id": "missing", "resolved": 0}] + [{"id": f"target{i}", "resolved": 1} for i in range(20)], "incoming": []}

        api = FakeApi()
        result = benchmark.retrieve(api, "same", ["same"], "prewritten_keywords_links")
        self.assertEqual(result[:3], ["seed0", "seed1", "seed2"])
        self.assertEqual(len(result), 10)
        self.assertNotIn("missing", result)
        self.assertEqual(len(api.paths), 2)

    def test_independent_source_tally_uses_both_venues(self):
        rows = [{"raw": {"HomeTeam": "A", "AwayTeam": "B", "FTHG": "1", "FTAG": "0"}},
                {"raw": {"HomeTeam": "B", "AwayTeam": "A", "FTHG": "2", "FTAG": "2"}},
                {"raw": {"HomeTeam": "C", "AwayTeam": "D", "FTHG": "9", "FTAG": "9"}}]
        self.assertEqual(benchmark.independently_tally(rows, "A"), {"played": 2, "won": 1, "drawn": 1,
                         "lost": 0, "goals_for": 3, "goals_against": 2, "points": 4, "goal_difference": 1})

    def test_nearest_rank_percentiles(self):
        actual = benchmark.distribution(list(range(1, 21)))
        self.assertEqual(actual["p50"], 10)
        self.assertEqual(actual["p95"], 19)
        self.assertEqual(actual["p99"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
