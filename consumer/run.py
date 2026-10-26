"""Run the consumer over a JSON Lines file of question events.

    python -m consumer.run --input events.jsonl --db answers.sqlite --backend fake
    python -m consumer.run --input events.jsonl --db answers.sqlite            # OE_AGENT_BACKEND, default anthropic

One ToolAgent is built once (same backends as the service: anthropic,
bedrock, fake) and its backend is checked for readiness BEFORE the first event
is fetched — a missing key or package is a configuration error and stops the
run with exit code 2, not a retry-and-dead-letter of every event. The file is
loaded into a MemorySource with queue semantics; the worker runs until the
queue is empty. Metrics are printed as one JSON object. Exit code 1 if
anything was dead-lettered — the dead letters are in the database with their
reasons.

Events are fetched one at a time by default (--batch-size 1), so the
visibility timeout applies per event: an agent call can take several model
round-trips, and a batch hidden together would expire together.

Configuration from the environment, as elsewhere in the repository:
OE_AGENT_BACKEND, OE_INDEX_DIR, OE_AGENT_MODEL, OE_BEDROCK_MODEL_ID; keys and
credentials never on the command line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from openedge_agent.agent3 import ToolAgent
from openedge_agent.llm import BACKENDS, make_llm

from .source import FileSource
from .store import AnswerStore
from .worker import Backoff, Worker


def build_agent(backend: str | None, index_dir: str | Path, k: int,
                max_steps: int) -> ToolAgent:
    index_dir = Path(index_dir)
    if not (index_dir / "chunks.json").exists():
        _config_error(f"no index at {index_dir}; run: python -m openedge_agent.ingest")
    llm = make_llm(backend)
    # readiness: the live backends create their client lazily and raise on a
    # missing key, model id or package — surface that now, before the queue
    ensure = getattr(llm, "_ensure_client", None)
    if callable(ensure):
        try:
            ensure()
        except Exception as exc:  # noqa: BLE001 — configuration, reported and stopped
            _config_error(f"backend not ready: {exc}")
    return ToolAgent(index_dir=index_dir, llm=llm, k=k, max_steps=max_steps)


def _config_error(message: str) -> None:
    """A configuration error: say it, exit 2 (argparse's code), touch nothing."""
    print(message, file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m consumer.run",
                                 description="Consume question events; persist answers")
    ap.add_argument("--input", type=Path, required=True,
                    help="JSON Lines file, one event per line (UTF-8; a BOM is tolerated)")
    ap.add_argument("--db", type=Path, default=Path("answers.sqlite"))
    ap.add_argument("--backend", default=None, choices=BACKENDS,
                    help=f"one of {BACKENDS}; default OE_AGENT_BACKEND, then anthropic")
    ap.add_argument("--index", default=os.environ.get("OE_INDEX_DIR", "index"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--visibility", type=float, default=30.0,
                    help="visibility timeout in seconds for a fetched event")
    ap.add_argument("--log-level", default="INFO",
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = ap.parse_args(argv)

    if args.max_attempts < 1:
        ap.error("--max-attempts must be >= 1")
    if args.batch_size < 1:
        ap.error("--batch-size must be >= 1")
    if args.visibility <= 0:
        ap.error("--visibility must be > 0")
    if not args.input.is_file():
        ap.error(f"--input {args.input} is not a file")
    if args.db.parent and not args.db.parent.exists():
        ap.error(f"--db folder {args.db.parent} does not exist")

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(message)s", stream=sys.stderr)

    try:
        source = FileSource(args.input, visibility_timeout=args.visibility,
                            max_receive_count=args.max_attempts + 1)
    except UnicodeDecodeError as exc:
        _config_error(f"{args.input} is not UTF-8 ({exc.reason}); re-save it as UTF-8")
    agent = build_agent(args.backend, args.index, args.k, args.max_steps)
    store = AnswerStore(args.db)
    worker = Worker(agent, source, store, max_attempts=args.max_attempts,
                    backoff=Backoff(), batch_size=args.batch_size)
    try:
        metrics = worker.run()
    finally:
        store.close()
    out = metrics.as_dict()
    out["db"] = str(args.db)
    print(json.dumps(out))
    return 1 if (metrics.dead_lettered or metrics.redriven) else 0


if __name__ == "__main__":
    sys.exit(main())
