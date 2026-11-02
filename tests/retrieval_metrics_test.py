#!/usr/bin/env python3
"""Hand-calculated metric checks protect the evaluation's reported scores."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retrieval_evaluator", ROOT / "scripts/evaluate_retrieval.py")
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class RetrievalMetricsTests(unittest.TestCase):
    def test_rank_and_graded_discount(self):
        actual = evaluator.score(["irrelevant", "support", "answer"], {"answer": 2, "support": 1}, {"answer"})
        self.assertEqual(actual["hit@1"], 0)
        self.assertEqual(actual["recall@5"], 1)
        self.assertEqual(actual["precision@5"], 2 / 5)
        self.assertEqual(actual["mrr@10"], 1 / 2)
        expected = (1 / math.log2(3) + 3 / math.log2(4)) / (3 + 1 / math.log2(3))
        self.assertAlmostEqual(actual["ndcg@5"], expected)
        self.assertEqual(actual["evidence_complete@1"], 0)
        self.assertEqual(actual["evidence_complete@5"], 1)

    def test_partial_multi_document_evidence_is_not_complete(self):
        actual = evaluator.score(["a"], {"a": 2, "b": 2}, {"a", "b"})
        self.assertEqual(actual["hit@1"], 1)
        self.assertEqual(actual["recall@5"], .5)
        self.assertEqual(actual["evidence_complete@5"], 0)
        self.assertAlmostEqual(actual["ndcg@5"], 3 / (3 + 3 / math.log2(3)))

    def test_empty_retrieval_is_a_miss_for_answerable_queries(self):
        actual = evaluator.score([], {"a": 2}, {"a"})
        self.assertTrue(all(value == 0 for value in actual.values()))

    def test_unknowns_are_excluded_from_positive_accuracy(self):
        self.assertIsNone(evaluator.score([], {}, {}))
        positive = evaluator.score(["a"], {"a": 2}, {"a"})
        rows = [{"metrics": positive, "retrieved": ["a"], "latency_ms": 1, "retrieval_http_calls": 1},
                {"metrics": None, "retrieved": [], "latency_ms": 2, "retrieval_http_calls": 1},
                {"metrics": None, "retrieved": ["wrong"], "latency_ms": 3, "retrieval_http_calls": 1}]
        actual = evaluator.summarize(rows)
        self.assertEqual(actual["metrics"]["recall@5"], 1)
        self.assertEqual(actual["answerable_queries"], 1)
        self.assertEqual(actual["unknown_empty_rate"], .5)

    def test_duplicate_results_cannot_inflate_scores(self):
        with self.assertRaises(ValueError):
            evaluator.score(["a", "a"], {"a": 2}, {"a"})

    def test_gold_quote_must_exist_in_source(self):
        fixture = json.loads((ROOT / "tests/fixtures/retrieval-v1.json").read_text())
        evaluator.validate_fixture(fixture)
        changed = copy.deepcopy(fixture)
        first = changed["queries"][0]
        first["evidence"][next(iter(first["evidence"]))] = ["This answer was invented."]
        with self.assertRaisesRegex(ValueError, "quote absent"):
            evaluator.validate_fixture(changed)

    def test_missing_source_is_not_a_valid_judgment(self):
        fixture = json.loads((ROOT / "tests/fixtures/retrieval-v1.json").read_text())
        fixture["queries"][0]["relevance"]["nonexistent/source"] = 2
        with self.assertRaisesRegex(ValueError, "Unknown relevance document"):
            evaluator.validate_fixture(fixture)


if __name__ == "__main__":
    unittest.main(verbosity=2)
