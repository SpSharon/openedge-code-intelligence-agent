"""An MCP server over the Stage 3 agent's three tools.

`read_unit`, `walk_calls` and `search` — the same code the agent loop calls,
over the same frozen Stage 1 index — exposed through the Model Context
Protocol so an MCP client (Claude Code, Claude Desktop, another agent) can use
them directly. Stateless by design: the read-set guarantee (`citations ⊆
read_set`) belongs to the agent loop in `openedge_agent/agent3.py`, not to this
server; see README.md, "The MCP server".
"""

from .server import SERVER_NAME, build_server, main

__all__ = ["SERVER_NAME", "build_server", "main"]
