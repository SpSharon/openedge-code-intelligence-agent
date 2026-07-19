"""OpenEdge code-intelligence agent - Stage 1: ingest, retrieval, scoreboard.

Zero-install layout: run from the repo root with
    python -m openedge_agent.ingest      # corpus -> index/
    python -m openedge_agent.score       # eval set -> saved scoreboard
    python -m openedge_agent.retrieve "what calls ar-invoice.p?"

Modules:
    abl.py        ABL-aware parsing (chunking, .df schema, calls, includes)
    ingest.py     corpus -> index/ artifacts
    retrieve.py   hybrid BM25 + local-embedding retrieval (RRF fusion)
    score.py      eval-set scoreboard (recall@k / precision / MRR)
"""

__version__ = "0.1.0"
