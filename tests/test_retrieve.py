"""Retrieval tests over the real ingested index (built by test setup if
missing). LSA embeddings build in well under a second on this corpus."""

import unittest
from pathlib import Path

from openedge_agent.ingest import build_index
from openedge_agent.retrieve import Retriever, tokenize

ROOT = Path(__file__).resolve().parents[1]


class TestRetrieve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "index/embed_meta.json").exists():
            build_index(ROOT, embed=True)
        cls.r = Retriever(index_dir=ROOT / "index", mode="hybrid")

    def test_tokenizer_identifiers_and_stems(self):
        toks = tokenize("RUN oe/oe-credit.p — cancelled procedures")
        self.assertIn("oe/oe-credit.p", toks)   # whole identifier survives
        self.assertIn("credit", toks)           # split part
        self.assertIn("cancel", toks)           # double-consonant stem
        self.assertIn("procedure", toks)        # -es stem keeps the 'e'

    def test_exact_name_query_hits_unit(self):
        top = [h["id"] for h in self.r.search("check-credit", k=3)]
        self.assertIn("corpus/oe/oe-credit.p#check-credit", top)

    def test_schema_query(self):
        top = [h["id"] for h in self.r.search(
            "Which fields does the OrderLine table have?", k=5)]
        self.assertIn("schema:OrderLine", top)

    def test_item_writers_query_surfaces_real_writers(self):
        top5 = [h["id"] for h in self.r.search(
            "Which procedures update the Item table?", k=5)]
        real_writer_files = {"corpus/inv/inv-alloc.p", "corpus/oe/oe-ship.p"}
        hits = {t.split("#")[0] for t in top5} & real_writer_files
        self.assertTrue(hits, f"no real Item writer in top5: {top5}")

    def test_deterministic(self):
        q = "What happens when an order is posted?"
        self.assertEqual(
            [h["id"] for h in self.r.search(q, k=10)],
            [h["id"] for h in self.r.search(q, k=10)],
        )

    def test_all_modes_run(self):
        for mode in ("bm25", "embed", "hybrid"):
            r = Retriever(index_dir=ROOT / "index", mode=mode)
            self.assertTrue(r.search("credit limit", k=3))


if __name__ == "__main__":
    unittest.main()
