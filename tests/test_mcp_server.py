"""Offline tests for mcp_server/ — the agent's three tools served over MCP to an
in-memory client, plus the server started as a real subprocess over stdio (no
network, no model). Skips cleanly when the MCP SDK is not installed, the way
the service tests skip without FastAPI."""

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:  # the MCP SDK is optional for the core suite (numpy only)
    import mcp  # noqa: F401
    from mcp.client import Client
except ImportError as exc:
    raise unittest.SkipTest(
        f"the MCP server tests need the mcp package ({exc}); "
        "pip install -r mcp_server/requirements.txt"
    )

from mcp_server import SERVER_NAME, build_server
from mcp_server.server import CorpusTools
from openedge_agent.retrieve import Retriever

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index"
TOOL_NAMES = {"read_unit", "walk_calls", "search", "list_units"}
UNIT = "corpus/oe/oe-entry.p#main"


def call(coro):
    return asyncio.run(coro)


async def _call(server, name, args):
    async with Client(server) as client:
        result = await client.call_tool(name, args)
    text = result.content[0].text
    try:
        return result, json.loads(text)
    except json.JSONDecodeError:  # a protocol error carries plain text
        return result, text


class ServerSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (INDEX / "chunks.json").exists():
            raise unittest.SkipTest("index/ missing — run python -m openedge_agent.ingest")
        cls.server = build_server(INDEX)
        cls.units = {u["id"] for u in Retriever(index_dir=INDEX).units}

    def test_lists_exactly_the_agents_tools_plus_list_units(self):
        async def go():
            async with Client(self.server) as client:
                return await client.list_tools()
        listed = call(go())
        names = {t.name for t in listed.tools}
        self.assertEqual(names, TOOL_NAMES)
        by_name = {t.name: t for t in listed.tools}
        self.assertEqual(set(by_name["read_unit"].input_schema["required"]), {"unit_id"})
        self.assertEqual(set(by_name["walk_calls"].input_schema["properties"]), {"unit_id", "direction"})
        self.assertEqual(set(by_name["search"].input_schema["required"]), {"query"})
        for t in listed.tools:
            self.assertTrue(t.description, f"{t.name} has no description")

    def test_server_identity(self):
        self.assertEqual(self.server.name, SERVER_NAME)
        self.assertIn("read_unit", self.server.instructions)
        self.assertIn("synthetic", self.server.instructions)

    def test_read_unit_returns_the_indexed_text(self):
        result, data = call(_call(self.server, "read_unit", {"unit_id": UNIT}))
        self.assertFalse(result.is_error)
        self.assertTrue(data["ok"])
        self.assertEqual(data["unit_id"], UNIT)
        expected = next(u for u in Retriever(index_dir=INDEX).units if u["id"] == UNIT)
        self.assertEqual(data["text"], expected["text"])
        self.assertEqual(data["chars"], len(expected["text"]))

    def test_read_unit_is_stateless_a_second_read_returns_the_text_again(self):
        _, first = call(_call(self.server, "read_unit", {"unit_id": UNIT}))
        _, second = call(_call(self.server, "read_unit", {"unit_id": UNIT}))
        self.assertIn("text", first)
        self.assertIn("text", second)
        self.assertEqual(first["text"], second["text"])
        self.assertNotIn("note", second, "the loop's 'already in your context' note must not leak")

    def test_bare_file_path_resolves_to_main(self):
        _, data = call(_call(self.server, "read_unit", {"unit_id": "corpus/oe/oe-entry.p"}))
        self.assertTrue(data["ok"])
        self.assertEqual(data["unit_id"], UNIT)

    def test_unknown_unit_is_a_result_not_an_exception(self):
        result, data = call(_call(self.server, "read_unit", {"unit_id": "no-such-unit.p"}))
        self.assertFalse(result.is_error, "an unknown id is an answer (ok=false), not a protocol error")
        self.assertFalse(data["ok"])
        self.assertIn("unknown unit id", data["error"])
        self.assertTrue(data["did_you_mean"])
        self.assertTrue(set(data["did_you_mean"]) <= self.units | {u.split("#")[0] for u in self.units})

    def test_walk_calls_returns_edges_and_no_unit_body(self):
        _, data = call(_call(self.server, "walk_calls", {"unit_id": UNIT, "direction": "out"}))
        self.assertTrue(data["ok"])
        self.assertTrue(data["calls_out"], "oe-entry.p#main runs other units")
        for edge in data["calls_out"]:
            self.assertIn(edge["unit_id"], self.units)
            self.assertNotIn("text", edge)
        self.assertIn("unresolved", data)
        self.assertIn("no unit body", data["note"])

    def test_walk_calls_rejects_a_bad_direction_without_raising(self):
        result, data = call(_call(self.server, "walk_calls", {"unit_id": UNIT, "direction": "sideways"}))
        self.assertFalse(result.is_error)
        self.assertFalse(data["ok"])
        self.assertIn("bad direction", data["error"])

    def test_search_returns_headers_only(self):
        _, data = call(_call(self.server, "search", {"query": "what calls ar-invoice.p?"}))
        self.assertTrue(data["ok"])
        self.assertTrue(1 <= len(data["hits"]) <= 5)
        for hit in data["hits"]:
            self.assertIn(hit["unit_id"], self.units)
            self.assertIn("header", hit)
            self.assertNotIn("text", hit, "search never returns a unit body")
        self.assertIn("corpus/ar/ar-invoice.p#main", [h["unit_id"] for h in data["hits"]])

    def test_list_units_matches_the_index(self):
        _, data = call(_call(self.server, "list_units", {}))
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], len(self.units))
        self.assertEqual({u["unit_id"] for u in data["units"]}, self.units)
        for u in data["units"]:
            self.assertNotIn("text", u)

    def test_missing_argument_is_a_protocol_error(self):
        with self.assertLogs("mcp", level="INFO"):   # the SDK logs the rejection; keep it out of the run's stderr
            result, text = call(_call(self.server, "read_unit", {}))
        self.assertTrue(result.is_error)
        self.assertIn("unit_id", str(text))

    def test_parallel_calls_never_see_each_others_state(self):
        """An MCP client may issue several calls at once and the SDK serves
        each on its own thread: forty concurrent read_unit calls of the same
        id must all return the text, never the loop's 'already in your
        context' note that a shared read set would produce."""
        async def go():
            async with Client(self.server) as client:
                results = await asyncio.gather(*[
                    client.call_tool("read_unit", {"unit_id": UNIT}) for _ in range(40)])
            return [json.loads(r.content[0].text) for r in results]
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)     # make thread preemption frequent
        try:
            data = call(go())
        finally:
            sys.setswitchinterval(old)
        self.assertEqual(len(data), 40)
        for d in data:
            self.assertTrue(d["ok"])
            self.assertIn("text", d)
            self.assertNotIn("note", d)


class StdioTransportTests(unittest.TestCase):
    """The way Claude Code starts it: `python -m mcp_server` as a subprocess,
    talking JSON-RPC over stdio."""

    def test_the_server_starts_as_a_subprocess_and_serves_its_tools(self):
        if not (INDEX / "chunks.json").exists():
            raise unittest.SkipTest("index/ missing — run python -m openedge_agent.ingest")
        from mcp.client.stdio import StdioServerParameters
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "mcp_server", "--index", str(INDEX)],
                                       cwd=str(ROOT))

        async def go():
            async with Client(params) as client:
                listed = await client.list_tools()
                result = await client.call_tool("search", {"query": "credit check"})
                return listed, json.loads(result.content[0].text), client.instructions
        listed, data, instructions = call(go())
        self.assertEqual({t.name for t in listed.tools}, TOOL_NAMES)
        self.assertTrue(data["ok"] and data["hits"])
        self.assertIn("read_unit", instructions or "")

    def test_runs_as_a_file_from_any_directory_with_the_index_found_by_location(self):
        """How a client outside the repository starts it (Claude Desktop):
        python <repo>/mcp_server/server.py, from some other working directory,
        no --index — the repository root is put on sys.path and index/ is
        found next to the package, not under the working directory."""
        if not (INDEX / "chunks.json").exists():
            raise unittest.SkipTest("index/ missing — run python -m openedge_agent.ingest")
        from mcp.client.stdio import StdioServerParameters
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(ROOT / "mcp_server" / "server.py")],
                                       cwd=tempfile.gettempdir())

        async def go():
            async with Client(params) as client:
                result = await client.call_tool("list_units", {})
                return json.loads(result.content[0].text)
        data = call(go())
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], len({u["id"] for u in Retriever(index_dir=INDEX).units}))

    def test_help_from_another_directory_names_the_repository_index(self):
        out = subprocess.run([sys.executable, str(ROOT / "mcp_server" / "server.py"), "--help"],
                             cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--index", out.stdout)


class CorpusToolsTests(unittest.TestCase):
    """The stateless wrapper, without the protocol in the way."""

    @classmethod
    def setUpClass(cls):
        if not (INDEX / "chunks.json").exists():
            raise unittest.SkipTest("index/ missing — run python -m openedge_agent.ingest")
        cls.tools = CorpusTools(INDEX)

    def test_every_call_gets_private_state_and_the_shared_agent_stays_clean(self):
        first = self.tools.read_unit(UNIT)
        self.assertIn("text", first)
        self.assertEqual(list(self.tools.agent.read_set), [], "the shared agent's read set is never written")
        self.tools.search("order entry")
        self.assertEqual(self.tools.agent.seen_ids, set(), "nor its seen set")
        second = self.tools.read_unit(UNIT)
        self.assertEqual(second["text"], first["text"])
        self.assertNotIn("note", second)

    def test_a_view_shares_the_index_but_not_the_state(self):
        a, b = self.tools._fresh(), self.tools._fresh()
        self.assertIs(a.retriever, b.retriever)
        self.assertIs(a.callgraph, b.callgraph)
        a.tool_read_unit(UNIT, step=1)
        self.assertEqual(list(a.read_set), [UNIT])
        self.assertEqual(list(b.read_set), [])
        self.assertEqual(list(self.tools.agent.read_set), [])

    def test_the_fake_llm_is_never_called(self):
        self.tools.read_unit(UNIT)
        self.tools.walk_calls(UNIT, "both")
        self.tools.search("invoice posting")
        self.tools.list_units()
        self.assertEqual(self.tools.agent.llm.prompts, [])

    def test_build_server_needs_an_index(self):
        with self.assertRaises(FileNotFoundError):
            build_server(ROOT / "no-such-index")


if __name__ == "__main__":
    unittest.main()
