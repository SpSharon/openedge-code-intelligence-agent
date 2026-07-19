"""Integration tests: full ingest over the real corpus (no embeddings — the
parsing artifacts are what's under test here)."""

import json
import unittest
from pathlib import Path

from openedge_agent.ingest import build_index

ROOT = Path(__file__).resolve().parents[1]


class TestIngest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = build_index(ROOT, embed=False)
        cls.callgraph = json.loads((ROOT / "index/callgraph.json").read_text())
        cls.chunks = {
            c["id"]: c
            for c in json.loads((ROOT / "index/chunks.json").read_text())
        }
        cls.schema = json.loads((ROOT / "index/schema.json").read_text())

    def test_counts(self):
        self.assertEqual(self.summary["files"], 13)      # 10 .p + 1 .cls + 2 .i (.df parsed separately)
        self.assertEqual(self.summary["tables"], 6)
        self.assertEqual(self.summary["sequences"], 2)
        self.assertEqual(self.summary["schema_units"], 8)
        self.assertEqual(self.summary["dynamic_edges"], 1)  # RUN VALUE in oe-post

    def test_run_in_handle_resolved(self):
        e = [
            e
            for e in self.callgraph["edges"]
            if e["type"] == "run_in_handle"
            and e["from"] == "corpus/oe/oe-entry.p#add-order-line"
        ]
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["resolved"], "corpus/inv/inv-alloc.p#allocate-item")

    def test_deallocate_edge_from_cancel(self):
        e = [
            e
            for e in self.callgraph["edges"]
            if e["type"] == "run_in_handle"
            and e["from"] == "corpus/oe/oe-cancel.p#main"
        ]
        self.assertEqual(e[0]["resolved"], "corpus/inv/inv-alloc.p#deallocate-item")

    def test_cls_delegation_edges(self):
        targets = {
            e["from"]: e["resolved"]
            for e in self.callgraph["edges"]
            if e["type"] == "run_external"
            and e["from"].startswith("corpus/service/OrderService.cls")
        }
        self.assertEqual(
            targets,
            {
                "corpus/service/OrderService.cls#CreateOrder": "corpus/oe/oe-entry.p",
                "corpus/service/OrderService.cls#CancelOrder": "corpus/oe/oe-cancel.p",
            },
        )

    def test_dynamic_edge_never_resolved(self):
        dyn = [e for e in self.callgraph["edges"] if e["type"] == "run_dynamic"]
        self.assertEqual(len(dyn), 1)
        self.assertIsNone(dyn[0]["resolved"])
        self.assertEqual(dyn[0]["from"], "corpus/oe/oe-post.p#main")

    def test_item_writers_exclude_post_trap(self):
        touches = self.callgraph["table_touches"]
        writers = {uid for uid, t in touches.items() if "Item" in t["written"]}
        self.assertEqual(
            writers,
            {
                "corpus/inv/inv-alloc.p#allocate-item",
                "corpus/inv/inv-alloc.p#deallocate-item",
                "corpus/oe/oe-ship.p#ship-line",
            },
        )

    def test_order_writers(self):
        touches = self.callgraph["table_touches"]
        writer_files = {
            uid.split("#")[0]
            for uid, t in touches.items()
            if "Order" in t["written"]
        }
        self.assertEqual(
            writer_files,
            {
                "corpus/oe/oe-entry.p",
                "corpus/oe/oe-ship.p",
                "corpus/oe/oe-post.p",
                "corpus/oe/oe-cancel.p",
            },
        )

    def test_include_chunk_ids_are_bare_paths(self):
        self.assertIn("corpus/include/oeshared.i", self.chunks)
        self.assertIn("corpus/include/ordstat.i", self.chunks)

    def test_corpus_readme_not_indexed(self):
        self.assertFalse(any("README" in cid for cid in self.chunks))

    def test_schema_unit_text_mentions_writers(self):
        item = next(
            u for u in self.schema["units"] if u["id"] == "schema:Item"
        )
        self.assertIn("oe-ship.p#ship-line", item["text"])
        self.assertNotIn("oe-post", item["text"])  # trap stays out


if __name__ == "__main__":
    unittest.main()
