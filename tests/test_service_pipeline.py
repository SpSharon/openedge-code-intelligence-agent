"""Offline tests for service/pipeline.py over the archived scoreboards."""

import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path

try:  # service deps are optional for the core suite (numpy only)
    import pandas  # noqa: F401
except ImportError as exc:
    raise unittest.SkipTest(
        f"service tests need pandas ({exc}); "
        "pip install -r service/requirements.txt"
    )

from service.pipeline import cases_table, load_scoreboards, main, runs_table

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"


class ServicePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.boards = load_scoreboards(RESULTS)

    def test_runs_table_one_row_per_scoreboard(self):
        n_files = len(list(RESULTS.glob("answers_scoreboard_*.json")))
        df = runs_table(self.boards)
        self.assertEqual(len(df), n_files)
        self.assertIn("answer_correctness", df.columns)
        heldout = df[df["label"].isin(
            ["stage3_heldout_r1", "stage3_heldout_r2", "stage3_heldout_r3"])]
        self.assertEqual(len(heldout), 3)
        self.assertTrue((heldout["answer_correctness"] == 1.0).all())

    def test_cases_table_counts_every_case(self):
        df = cases_table(self.boards)
        self.assertEqual(len(df), sum(len(b["cases"]) for b in self.boards))

    def test_main_writes_a_csv_without_an_index_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "runs.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main([str(RESULTS), "--out", str(out)])
            self.assertEqual(code, 0)
            with open(out, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0][:3], ["label", "run_at", "model"])
        self.assertEqual(len(rows) - 1, len(self.boards))
        # a folder with no scoreboards: message on stderr, exit 1, no CSV
        with tempfile.TemporaryDirectory() as empty:
            out = Path(empty) / "runs.csv"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main([empty, "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertIn("no answers_scoreboard_", err.getvalue())
            self.assertFalse(out.exists())

    def test_older_scoreboard_missing_fields_gives_nan_not_error(self):
        board = {"label": "old", "run_at": "2026-07-01", "llm": {"model": "m"},
                 "params": {"k": 10}, "metrics": {"n_cases": 1},
                 "cases": [{"id": "X1", "citations": []}]}
        df = runs_table([board])
        self.assertTrue(df["answer_correctness"].isna().all())
        self.assertTrue(df["agent"].isna().all())
        self.assertEqual(len(cases_table([board])), 1)


if __name__ == "__main__":
    unittest.main()
