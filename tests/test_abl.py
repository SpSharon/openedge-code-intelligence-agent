"""Unit tests for ABL parsing. Stdlib unittest (PyPI was unreachable in the
build environment, so no pytest); `python -m unittest discover tests` from
the repo root."""

import unittest
from pathlib import Path

from openedge_agent import abl

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text()


class TestStripper(unittest.TestCase):
    def test_nested_comments_blanked(self):
        src = "A /* outer /* inner */ still comment */ B"
        out = abl.strip_comments_and_strings(src)
        self.assertIn("A", out)
        self.assertIn("B", out)
        self.assertNotIn("inner", out)
        self.assertNotIn("comment", out)
        self.assertEqual(len(out), len(src))

    def test_strings_blanked_lines_preserved(self):
        src = 'MESSAGE "RUN oe/fake.p".\nRUN real-proc.\n'
        out = abl.strip_comments_and_strings(src)
        self.assertNotIn("fake", out)
        self.assertIn("RUN real-proc", out)
        self.assertEqual(src.count("\n"), out.count("\n"))

    def test_doubled_quote_escape(self):
        src = 'X = "he said ""hi"" ok".\nY = 2.'
        out = abl.strip_comments_and_strings(src)
        self.assertIn("Y = 2", out)
        self.assertNotIn("hi", out)


class TestChunker(unittest.TestCase):
    def test_oe_entry_units(self):
        units = abl.chunk_source(
            "corpus/oe/oe-entry.p", read("corpus/oe/oe-entry.p")
        )
        ids = {u.id for u in units}
        self.assertIn("corpus/oe/oe-entry.p#main", ids)
        self.assertIn("corpus/oe/oe-entry.p#create-order-header", ids)
        self.assertIn("corpus/oe/oe-entry.p#add-order-line", ids)
        self.assertEqual(len(units), 3)

    def test_function_and_procedure_kinds(self):
        units = abl.chunk_source(
            "corpus/oe/oe-credit.p", read("corpus/oe/oe-credit.p")
        )
        kinds = {u.name: u.kind for u in units}
        self.assertEqual(kinds["get-open-balance"], "function")
        self.assertEqual(kinds["check-credit"], "procedure")

    def test_cls_methods(self):
        units = abl.chunk_source(
            "corpus/service/OrderService.cls",
            read("corpus/service/OrderService.cls"),
        )
        names = {u.name for u in units if u.kind == "method"}
        self.assertEqual(names, {"CreateOrder", "CancelOrder", "GetOrderTotal"})

    def test_include_is_single_chunk_with_bare_path_id(self):
        units = abl.chunk_source(
            "corpus/include/oeshared.i", read("corpus/include/oeshared.i")
        )
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].id, "corpus/include/oeshared.i")
        self.assertEqual(units[0].kind, "include")


class TestDfParser(unittest.TestCase):
    def setUp(self):
        self.schema = abl.parse_df(read("corpus/db/ordermgmt.df"))

    def test_tables_and_sequences(self):
        self.assertEqual(
            set(self.schema["tables"]),
            {"Customer", "Order", "OrderLine", "Item", "SalesRep", "ArHist"},
        )
        self.assertEqual(
            set(self.schema["sequences"]), {"next-ord-num", "next-inv-num"}
        )

    def test_orderline_primary_index(self):
        ol = self.schema["tables"]["OrderLine"]
        prim = [i for i in ol["indexes"] if i["primary"]][0]
        self.assertTrue(prim["unique"])
        self.assertEqual(prim["fields"], ["OrderNum", "LineNum"])
        self.assertEqual(len(ol["fields"]), 8)

    def test_extent_field(self):
        sr = self.schema["tables"]["SalesRep"]
        quota = [f for f in sr["fields"] if f["name"] == "MonthQuota"][0]
        self.assertEqual(quota.get("extent"), "12")


class TestCallExtraction(unittest.TestCase):
    def _unit_stripped(self, rel: str, unit_name: str) -> tuple[str, list]:
        text = read(rel)
        stripped = abl.strip_comments_and_strings(text).splitlines()
        units = abl.chunk_source(rel, text)
        functions = [u.name for u in units if u.kind == "function"]
        u = next(u for u in units if u.name == unit_name)
        su = "\n".join("\n".join(stripped[s - 1 : e]) for s, e in u.segments)
        return su, functions

    def test_oe_entry_main_calls_credit(self):
        su, fns = self._unit_stripped("corpus/oe/oe-entry.p", "main")
        edges = abl.extract_calls(su, fns)
        ext = [e for e in edges if e["type"] == "run_external"]
        self.assertEqual([e["target"] for e in ext], ["oe/oe-credit.p"])
        internal = {e["target"] for e in edges if e["type"] == "run_internal"}
        self.assertEqual(internal, {"create-order-header", "add-order-line"})

    def test_add_order_line_calls(self):
        su, fns = self._unit_stripped("corpus/oe/oe-entry.p", "add-order-line")
        edges = abl.extract_calls(su, fns)
        ext = {e["target"]: e for e in edges if e["type"] == "run_external"}
        self.assertIn("oe/oe-price.p", ext)
        self.assertIn("inv/inv-alloc.p", ext)
        self.assertTrue(ext["inv/inv-alloc.p"]["persistent"])
        handle = [e for e in edges if e["type"] == "run_in_handle"]
        self.assertEqual(len(handle), 1)
        self.assertEqual(handle[0]["target"], "allocate-item")
        self.assertEqual(handle[0]["handle"], "gh-alloc")

    def test_oe_post_dynamic_and_invoice(self):
        su, fns = self._unit_stripped("corpus/oe/oe-post.p", "main")
        edges = abl.extract_calls(su, fns)
        types = {e["type"] for e in edges}
        self.assertIn("run_dynamic", types)
        ext = {e["target"] for e in edges if e["type"] == "run_external"}
        self.assertEqual(ext, {"ar/ar-invoice.p"})

    def test_function_call_edge(self):
        su, fns = self._unit_stripped("corpus/oe/oe-credit.p", "check-credit")
        edges = abl.extract_calls(su, fns)
        fcalls = {e["target"] for e in edges if e["type"] == "function_call"}
        self.assertEqual(fcalls, {"get-open-balance"})

    def test_sequence_refs(self):
        su, _ = self._unit_stripped("corpus/oe/oe-entry.p", "create-order-header")
        self.assertEqual(abl.extract_sequence_refs(su), ["next-ord-num"])


class TestTableTouches(unittest.TestCase):
    TABLES = {"Customer", "Order", "OrderLine", "Item", "SalesRep", "ArHist"}

    def _touches(self, rel: str, unit_name: str, extra_alias_src: str = ""):
        text = read(rel)
        stripped_full = abl.strip_comments_and_strings(text)
        stripped = stripped_full.splitlines()
        units = abl.chunk_source(rel, text)
        u = next(u for u in units if u.name == unit_name)
        su = "\n".join("\n".join(stripped[s - 1 : e]) for s, e in u.segments)
        aliases = abl.buffer_aliases(stripped_full, self.TABLES)
        if extra_alias_src:
            aliases.update(
                abl.buffer_aliases(
                    abl.strip_comments_and_strings(read(extra_alias_src)),
                    self.TABLES,
                )
            )
        return abl.table_touches(su, self.TABLES, aliases)

    def test_oe_post_trap_no_item_write(self):
        """The pre-2007 Item update survives only in a comment: the
        structured analysis must NOT see oe-post.p writing Item."""
        t = self._touches("corpus/oe/oe-post.p", "main")
        self.assertIn("Order", t["written"])
        self.assertNotIn("Item", t["written"])

    def test_ship_line_writes_item_and_line(self):
        t = self._touches("corpus/oe/oe-ship.p", "ship-line")
        self.assertEqual(set(t["written"]), {"Item", "OrderLine"})

    def test_ar_invoice_writes(self):
        t = self._touches("corpus/ar/ar-invoice.p", "main")
        self.assertEqual(set(t["written"]), {"ArHist", "Customer"})

    def test_credit_check_reads_customer_via_shared_buffer(self):
        t = self._touches(
            "corpus/oe/oe-credit.p",
            "check-credit",
            extra_alias_src="corpus/include/oeshared.i",
        )
        self.assertIn("Customer", t["referenced"])
        self.assertEqual(t["written"], [])


if __name__ == "__main__":
    unittest.main()
