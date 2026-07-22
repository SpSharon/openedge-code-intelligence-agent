"""Tests for the Stage 2 answer scorer: metric math, must-mention
matching, and well-formedness of the answers eval set(s). Offline."""

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "answer_score", ROOT / "evals" / "answer_score.py"
)
answer_score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(answer_score)

score_citations = answer_score.score_citations
score_must_mention = answer_score.score_must_mention
gold_in_context = answer_score.gold_in_context
aggregate = answer_score.aggregate
load_cases = answer_score.load_cases


def _case(**kw):
    base = {
        "id": "T1",
        "category": "flow",
        "question": "q",
        "gold": ["corpus/a.p", "corpus/b.p#f"],
        "acceptable": ["schema:X"],
        "must_mention": [
            {"note": "fact one", "any": ["alpha"]},
            {"note": "fact two", "any": ["\\bbeta\\b", "betamax"]},
        ],
    }
    base.update(kw)
    return base


class TestScoreCitations(unittest.TestCase):
    def test_recall_uses_target_matches_semantics(self):
        c = _case()
        s = score_citations(c, ["corpus/a.p#main"])  # file-level gold hit
        self.assertEqual(s["n_gold_cited"], 1)
        self.assertEqual(s["gold_missed"], ["corpus/b.p#f"])

    def test_precision_counts_acceptable_as_good(self):
        c = _case()
        s = score_citations(c, ["schema:X", "corpus/z.p#main"])
        self.assertEqual(s["n_good_citations"], 1)
        self.assertEqual(s["bad_citations"], ["corpus/z.p#main"])
        self.assertEqual(s["n_gold_cited"], 0)

    def test_exact_unit_gold_not_satisfied_by_other_chunk(self):
        c = _case()
        s = score_citations(c, ["corpus/b.p#other"])
        self.assertIn("corpus/b.p#f", s["gold_missed"])
        # ...and #other is not gold/acceptable either
        self.assertEqual(s["bad_citations"], ["corpus/b.p#other"])

    def test_no_citations(self):
        s = score_citations(_case(), [])
        self.assertEqual(s["n_citations"], 0)
        self.assertEqual(s["n_gold_cited"], 0)


class TestMustMention(unittest.TestCase):
    def test_any_of_patterns_case_insensitive(self):
        s = score_must_mention(_case(), "It uses ALPHA throughout.")
        self.assertEqual(s["n_covered"], 1)
        self.assertEqual(s["missed"], ["fact two"])

    def test_word_boundary_pattern(self):
        s = score_must_mention(_case(), "alphabet betamax")
        # 'alphabet' contains 'alpha' (substring pattern -> hit);
        # 'betamax' hits fact two via its second alternative
        self.assertEqual(s["n_covered"], 2)
        self.assertEqual(s["coverage"], 1.0)

    def test_boundary_blocks_partial_word(self):
        s = score_must_mention(
            _case(must_mention=[{"note": "b", "any": ["\\bbeta\\b"]}]),
            "betamax only",
        )
        self.assertEqual(s["n_covered"], 0)


class TestAggregate(unittest.TestCase):
    def test_micro_and_macro_math(self):
        rows = [
            {
                "category": "flow", "answer": "x",
                "citations": [], "invalid_citations": [],
                "citation": {"n_gold": 2, "n_gold_cited": 2, "gold_missed": [],
                             "n_citations": 2, "n_good_citations": 2,
                             "bad_citations": []},
                "must_mention": {"n_facts": 2, "n_covered": 2, "coverage": 1.0,
                                 "missed": []},
                "context": {"n_gold_in_context": 2, "gold_absent": []},
            },
            {
                "category": "schema", "answer": "not in the retrieved code",
                "citations": [], "invalid_citations": ["fake:id"],
                "citation": {"n_gold": 2, "n_gold_cited": 1,
                             "gold_missed": ["g"], "n_citations": 2,
                             "n_good_citations": 1, "bad_citations": ["b"]},
                "must_mention": {"n_facts": 4, "n_covered": 1, "coverage": 0.25,
                                 "missed": ["a", "b", "c"]},
                "context": {"n_gold_in_context": 1, "gold_absent": ["g"]},
            },
        ]
        agg = aggregate(rows)["metrics"]
        self.assertAlmostEqual(agg["citation_recall"], 3 / 4)
        self.assertAlmostEqual(agg["citation_precision"], 3 / 4)
        self.assertAlmostEqual(agg["answer_correctness"], (1.0 + 0.25) / 2)
        self.assertEqual(agg["perfect_answer_cases"], 1)
        self.assertAlmostEqual(agg["gold_in_context_rate"], 3 / 4)
        self.assertEqual(agg["refusals"], 1)
        self.assertEqual(agg["invalid_citation_total"], 1)


class TestAnswersEvalSet(unittest.TestCase):
    """The dev rubric must stay aligned with the frozen retrieval set."""

    @classmethod
    def setUpClass(cls):
        cls.answer_cases = load_cases(ROOT / "evals/answers.jsonl")
        cls.retrieval_cases = load_cases(ROOT / "evals/retrieval.jsonl")

    def test_same_20_questions_and_gold_as_retrieval_set(self):
        self.assertEqual(len(self.answer_cases), 20)
        by_id = {c["id"]: c for c in self.retrieval_cases}
        for c in self.answer_cases:
            r = by_id[c["id"]]
            self.assertEqual(c["question"], r["question"], c["id"])
            self.assertEqual(c["gold"], r["gold"], c["id"])
            self.assertEqual(c.get("acceptable", []), r.get("acceptable", []),
                             c["id"])

    def test_every_case_has_valid_must_mention(self):
        for c in self.answer_cases:
            self.assertGreaterEqual(len(c["must_mention"]), 1, c["id"])
            for item in c["must_mention"]:
                self.assertTrue(item["note"], c["id"])
                self.assertGreaterEqual(len(item["any"]), 1, c["id"])
                for pat in item["any"]:
                    re.compile(pat, re.IGNORECASE)  # must compile

    def test_patterns_are_not_trivially_satisfied_by_the_question(self):
        """A rubric fact an answer covers by echoing the question back is
        not measuring correctness. Known, accepted exceptions are listed:
        each is a fact the question itself names (e.g. S5 asks for status
        values and 'ordered' appears in 'order status'); for those the
        real signal is the other facts + citations."""
        allowed = {
            ("S5", "Ordered"),        # 'ordered' ⊂ question's 'order status'
            ("F4", "line and header statuses are set to Cancelled"),
            ("F4", "only orders still in Ordered status can be cancelled (shipped/posted go to RMA)"),
            ("G4", "selects lines by backordered line status ({&LIN-BACKORD})"),
            ("C4", "inside the per-order posting flow (retry block / batch context)"),
        }
        for c in self.answer_cases:
            for item in c["must_mention"]:
                hit = any(
                    re.search(p, c["question"], re.IGNORECASE)
                    for p in item["any"]
                )
                if hit:
                    self.assertIn(
                        (c["id"], item["note"]), allowed,
                        f"{c['id']}: pattern for '{item['note']}' matches "
                        f"the question text itself",
                    )


def _index_ids():
    chunks = json.loads((ROOT / "index/chunks.json").read_text())
    schema = json.loads((ROOT / "index/schema.json").read_text())
    return [c["id"] for c in chunks] + [u["id"] for u in schema["units"]]


class TestHeldoutEvalSet(unittest.TestCase):
    """The held-out set must be structurally sound BEFORE any real run:
    resolvable targets, compiling patterns, no echo-scoring."""

    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "evals/answers_heldout.jsonl"
        cls.cases = load_cases(cls.path) if cls.path.exists() else []

    def _skip_if_absent(self):
        if not self.cases:
            self.skipTest("answers_heldout.jsonl not present yet")

    def test_shape(self):
        self._skip_if_absent()
        self.assertGreaterEqual(len(self.cases), 8)
        self.assertLessEqual(len(self.cases), 11)
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        refusals = [c for c in self.cases if c["category"] == "refusal"]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["gold"], [])

    def test_no_overlap_with_dev_questions(self):
        self._skip_if_absent()
        dev_qs = {c["question"] for c in load_cases(ROOT / "evals/answers.jsonl")}
        for c in self.cases:
            self.assertNotIn(c["question"], dev_qs, c["id"])

    def test_targets_resolve_against_the_index(self):
        self._skip_if_absent()
        index_ids = _index_ids()
        for c in self.cases:
            for t in c["gold"] + c.get("acceptable", []):
                if t.startswith("schema:") or "#" in t:
                    self.assertIn(t, index_ids, f"{c['id']}: {t}")
                else:  # file-level target: some chunk of that file must exist
                    self.assertTrue(
                        any(i == t or i.startswith(t + "#") for i in index_ids),
                        f"{c['id']}: {t}",
                    )

    def test_patterns_compile_and_do_not_echo_the_question(self):
        self._skip_if_absent()
        for c in self.cases:
            self.assertGreaterEqual(len(c["must_mention"]), 1, c["id"])
            for item in c["must_mention"]:
                for pat in item["any"]:
                    re.compile(pat, re.IGNORECASE)
                if c["category"] == "refusal":
                    continue  # the refusal phrase can't echo a question
                hit = any(
                    re.search(p, c["question"], re.IGNORECASE)
                    for p in item["any"]
                )
                self.assertFalse(
                    hit,
                    f"{c['id']}: pattern for '{item['note']}' matches the "
                    f"question text itself",
                )


class TestMarkdownRobustMatching(unittest.TestCase):
    """A correct answer must not lose a must_mention point just because it
    wrapped the key term in **bold** or `backticks` (real cases: H9, S2)."""

    def test_bold_and_backticks_do_not_break_matching(self):
        case = _case(must_mention=[
            {"note": "refusal phrase", "any": ["not in the retrieved code"]},
            {"note": "on the customer table", "any": ["customer table"]},
        ])
        ans = ("The system is **not** in the retrieved code; the field lives "
               "on the `Customer` table.")
        s = score_must_mention(case, ans)
        self.assertEqual(s["n_covered"], 2, s["missed"])


if __name__ == "__main__":
    unittest.main()
