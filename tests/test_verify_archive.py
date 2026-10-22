"""The archive must follow from the evidence: every archived answer scoreboard's
stored metrics re-derive under the scorer at HEAD, and the Stage 1 headline
scoreboard reproduces from the corpus (needs index/ — run ingest first, as the
whole suite does). Offline; no key, no network; nothing written."""

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# evals/ is not a package (same loading pattern as tests/test_answer_score.py)
_spec = importlib.util.spec_from_file_location(
    "verify_archive", ROOT / "evals" / "verify_archive.py"
)
va = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(va)


class ArchivedAnswerScoreboardsTests(unittest.TestCase):
    def test_every_archived_answer_scoreboard_reproduces(self):
        boards = va.answer_boards()
        self.assertGreaterEqual(len(boards), 13, "the archived scoreboards are part of the record")
        with redirect_stdout(io.StringIO()):
            checked, failed = va.verify_answers()
        self.assertEqual(checked, len(boards), "every archived board must be checked, not a sample")
        self.assertEqual(failed, 0)

    def test_every_archived_board_carries_its_provenance(self):
        """A number without its provenance cannot be quoted: every archived
        scoreboard names its eval set, its model and its metered usage, and
        none of them came from the fake backend."""
        for board in va.answer_boards():
            data = json.loads(board.read_text())
            self.assertIn("cases_file", data, board.name)
            self.assertTrue((ROOT / "evals" / data["cases_file"]).exists(), board.name)
            self.assertIn("metrics", data, board.name)
            self.assertFalse(data.get("fake"), board.name)
            for key in ("model", "calls", "input_tokens", "output_tokens"):
                self.assertIn(key, data["llm"], f"{board.name}: llm.{key}")
            self.assertNotEqual(data["llm"]["model"], "fake-llm", board.name)


class Stage1ScoreboardTests(unittest.TestCase):
    def test_stage1_headline_reproduces_from_the_corpus(self):
        if not (ROOT / "index" / "chunks.json").exists():
            self.skipTest("index/ missing — run python -m openedge_agent.ingest")
        archived = json.loads(va.STAGE1_ARCHIVE.read_text())
        if va.index_backend() != archived["embed_backend"]:
            self.skipTest(f"the local index was built with {va.index_backend()!r}, the archive "
                          f"with {archived['embed_backend']!r} — rebuild with the shipped defaults")
        before = sorted(p.name for p in (ROOT / "evals" / "results").iterdir())
        with redirect_stdout(io.StringIO()):
            rc = va.verify_stage1()
        self.assertEqual(rc, 0)
        after = sorted(p.name for p in (ROOT / "evals" / "results").iterdir())
        self.assertEqual(before, after, "the verifier must write nothing into evals/results/")

    def test_differences_report_every_key_except_the_timestamp(self):
        """Perturb each top-level key of the archived board in turn (and one
        per-case row): every one must be reported; only run_at is ignored."""
        archived = json.loads(va.STAGE1_ARCHIVE.read_text())
        same = dict(archived, run_at="2099-01-01T00:00:00+00:00")
        self.assertEqual(va.stage1_differences(same, archived), [])
        for key in archived:
            if key == va.TIMESTAMP_KEY:
                continue
            changed = json.loads(json.dumps(archived))
            if key == "cases":
                changed["cases"][0]["gold_ranks"] = {}
                expected = f"cases[{archived['cases'][0]['id']}]"
            else:
                changed[key] = "perturbed"
                expected = key
            self.assertIn(expected, va.stage1_differences(changed, archived), key)
        removed = {k: v for k, v in archived.items() if k != "metrics"}
        self.assertIn("metrics", va.stage1_differences(removed, archived))


if __name__ == "__main__":
    unittest.main()
