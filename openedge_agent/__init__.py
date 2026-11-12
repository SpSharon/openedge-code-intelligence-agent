"""OpenEdge code-intelligence agent: ingest, retrieval, scoreboard, and the two answer agents.

Zero-install layout: run from the repo root with
    python -m openedge_agent.ingest      # corpus -> index/
    python -m openedge_agent.score       # eval set -> saved scoreboard
    python -m openedge_agent.retrieve "what calls ar-invoice.p?"

Modules:
    abl.py        ABL-aware parsing (chunking, .df schema, calls, includes)
    ingest.py     corpus -> index/ artifacts
    retrieve.py   hybrid BM25 + local-embedding retrieval (RRF fusion)
    score.py      eval-set scoreboard (recall@k / precision / MRR)
    agent.py      Stage 2: single-shot grounded answers with validated citations
    agent3.py     Stage 3: the tool-using agent (read_unit / walk_calls / search)
    llm.py        pluggable model client (Anthropic, Bedrock, FakeLLM) + usage meter
"""

__version__ = "0.1.0"
