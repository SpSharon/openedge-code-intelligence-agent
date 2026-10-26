"""The MCP server: the Stage 3 agent's three tools, over stdio.

What it is
----------
`openedge_agent/agent3.py` gives the model three tools — `read_unit`,
`walk_calls`, `search` — as a text protocol inside its own loop (a `TOOL:`
line, a JSON result pasted back into the next prompt). This module exposes the
*same three methods* through the Model Context Protocol, so any MCP client can
call them: Claude Code (`.mcp.json` at the repository root), Claude Desktop, or
another agent. Nothing in the agent changes; the server imports `ToolAgent` and
calls its tool methods on the frozen index.

What it is not
--------------
It is not the agent. There is no model call, no loop, no budget and no answer
here — the client brings its own model and its own loop. In particular the
agent's structural guarantee that a citation can only name a unit it has read
(`citations ⊆ read_set`) lives in the loop, not in these tools: a client that
calls `read_unit` and then cites something else is outside this repository's
guarantee. The server is stateless on purpose — every call starts with an
empty read set, so `read_unit` always returns the unit's text, and `search` /
`walk_calls` return headers and edges only, never a unit body, exactly as they
do inside the agent.

Run
---
    pip install -r mcp_server/requirements.txt     # the official MCP SDK (2.x)
    python -m openedge_agent.ingest                # the index, once
    python -m mcp_server                           # from the repository root; stdio
    python <repo>/mcp_server/server.py             # from anywhere (a client outside the repo)
                                                   # --index DIR or OE_AGENT_INDEX; default: the repo's index/
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    # Run as a file (python path/to/mcp_server/server.py — how an MCP client
    # outside the repository starts it): make the repository importable.
    sys.path.insert(0, str(REPO_ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from openedge_agent.agent3 import ToolAgent  # noqa: E402
from openedge_agent.llm import FakeLLM  # noqa: E402

SERVER_NAME = "openedge-code-intelligence"
DEFAULT_INDEX = REPO_ROOT / "index"   # by file location, not by working directory

INSTRUCTIONS = """\
Tools over a synthetic Progress 4GL / OpenEdge (ABL) corpus, indexed by the
OpenEdge Code-Intelligence Agent (github.com/SpSharon/openedge-code-intelligence-agent).

- read_unit(unit_id): the full text of one code unit (a procedure, a class,
  an include file, or a schema table/sequence). The only tool that returns code.
- walk_calls(unit_id, direction): call-graph edges for a unit or a whole file
  (out = what it runs, in = what runs it, both). Ids and raw call targets only —
  no code. Call lists may be incomplete: dynamic RUN VALUE targets are never
  statically resolved, and the result says so.
- search(query): the top-5 unit headers from the agent's hybrid retriever
  (BM25 + local embeddings + call-graph structure). Headers and x-refs only — no
  code; read a hit with read_unit before relying on it.
- list_units(): every unit id with its kind and file, to orient.

The discipline the agent itself follows, offered here as advice: cite only
units whose text you have read with read_unit; a search hit or a call-graph
edge is a lead, not evidence. If the corpus does not contain the answer, say so
rather than infer it. Unit ids look like `corpus/oe/oe-entry.p#main` or
`schema:Customer`; a bare file path resolves to that file's #main unit.
"""


class CorpusTools:
    """The agent's tool methods, called statelessly.

    One `ToolAgent` is built over the index with a scripted FakeLLM that is
    never invoked — only the tool methods and the id-resolution maps are used.
    Every call gets its own shallow copy of the agent with an empty
    per-question state (`read_set`, `seen_ids`); the index, retriever and
    id maps are shared, the mutable state is not. So a second `read_unit` of
    the same id returns the text again instead of the loop's "already in your
    context" note, and parallel calls — an MCP client may issue several at
    once, and the SDK serves each on its own thread — cannot see each other's
    state. The shared agent's own state is never touched.
    """

    def __init__(self, index_dir: str | Path):
        self.index_dir = Path(index_dir)
        self.agent = ToolAgent(index_dir=self.index_dir, llm=FakeLLM(), k=10)

    def _fresh(self) -> ToolAgent:
        view = copy.copy(self.agent)          # shares the index; private state below
        view.read_set = OrderedDict()
        view.seen_ids = set()
        return view

    def read_unit(self, unit_id: str) -> dict[str, Any]:
        return self._fresh().tool_read_unit(unit_id, step=1)

    def walk_calls(self, unit_id: str, direction: str = "both") -> dict[str, Any]:
        return self._fresh().tool_walk_calls(unit_id, direction)

    def search(self, query: str) -> dict[str, Any]:
        return self._fresh().tool_search(query)

    def list_units(self) -> dict[str, Any]:
        units = [
            {"unit_id": u["id"], "kind": u["kind"], "name": u.get("name", ""), "file": u["file"]}
            for u in self.agent.retriever.units
        ]
        units.sort(key=lambda d: d["unit_id"])
        return {"ok": True, "count": len(units), "units": units,
                "note": "ids, kinds and files only, no unit body; read a unit with read_unit."}


def build_server(index_dir: str | Path = "index") -> MCPServer:
    """Build the server over `index_dir`; the index must already exist."""
    index_dir = Path(index_dir)
    if not (index_dir / "chunks.json").exists():
        raise FileNotFoundError(
            f"no index at {index_dir} — run `python -m openedge_agent.ingest` first"
        )
    tools = CorpusTools(index_dir)
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(name="read_unit")
    def read_unit(unit_id: str) -> dict[str, Any]:
        """Full text of one code unit — the only tool that returns code. Unit
        ids look like corpus/oe/oe-entry.p#main or schema:Customer; a bare file
        path resolves to that file's #main unit. An unknown id is answered with
        ok=false and, when anything is close, did_you_mean suggestions."""
        return tools.read_unit(unit_id)

    @server.tool(name="walk_calls")
    def walk_calls(unit_id: str, direction: str = "both") -> dict[str, Any]:
        """Call-graph edges for a unit or a whole file: direction out (what it
        runs), in (what runs it) or both. Ids and raw call targets only, no
        code; lists may be incomplete because dynamic RUN VALUE targets are
        never statically resolved (the result lists them under unresolved).
        Any other direction is answered with ok=false by the agent's own check."""
        return tools.walk_calls(unit_id, direction)

    @server.tool(name="search")
    def search(query: str) -> dict[str, Any]:
        """Top-5 unit headers from the agent's hybrid retriever for a free-text
        query (max 200 characters). Headers and x-refs only, no code — read a
        hit with read_unit before relying on it."""
        return tools.search(query)

    @server.tool(name="list_units")
    def list_units() -> dict[str, Any]:
        """Every unit id in the index with its kind and file — to orient
        before reading. No code."""
        return tools.list_units()

    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Serve the agent's three tools over MCP (stdio)")
    ap.add_argument("--index", default=os.environ.get("OE_AGENT_INDEX", str(DEFAULT_INDEX)),
                    help="index directory (default: $OE_AGENT_INDEX, else the repository's index/)")
    args = ap.parse_args(argv)
    build_server(args.index).run("stdio")


if __name__ == "__main__":
    main()
