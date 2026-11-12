"""Run the consumer against a Kafka topic.

    python -m consumer.kafka_run --topic oe.questions --db kafka-drill.sqlite --backend fake --idle-exit 15

The same worker, store and agent as `python -m consumer.run`; only the source
differs (consumer/kafka_source.py). Configuration is checked BEFORE the first
poll and stops the run with exit code 2: the confluent-kafka package missing,
the broker unreachable, the topic missing, the index or the model backend not
ready. Exit code 1 if anything was dead-lettered — those are in the database
with their reasons and on the dead-letter topic (default: <topic>.dlq) with the
reason in the message headers — or if the broker failed mid-run ("stopped: ..."
on stderr). Exit code 3 only from the die-before-commit drill.

A stream has no end, so the run stops only when asked: --idle-exit N stops
after N seconds without a message, counted from the moment the consumer group
has assigned this consumer its partitions (what the drills use); without it
the run goes on until Ctrl+C.

--drill is for the three drills in the README and nothing else:
    die-before-commit   exit hard after the first write, before its commit —
                        run again without the flag: the event is delivered
                        again, logged "duplicate / already stored", one row
                        (the second run first waits a few seconds for the dead
                        member's session to expire)
    fail-first-ask      the first model call raises a connection error once —
                        the event is retried after the backoff, attempt 2

Security settings (SASL, TLS) go in a JSON file passed as --client-config,
never on the command line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from openedge_agent.llm import BACKENDS

from .kafka_source import KafkaSource, KafkaSourceError
from .run import _config_error, build_agent
from .store import AnswerStore
from .worker import Backoff, Worker


class _FailFirstAsk:
    """Drill: the first ask() raises a transient error; the rest pass through."""

    def __init__(self, agent):
        self._agent = agent
        self._failed = False

    def ask(self, question: str):
        if not self._failed:
            self._failed = True
            raise ConnectionError("drill: fail-first-ask")
        return self._agent.ask(question)


def _die_before_commit(source: KafkaSource) -> None:
    """Drill: the process ends after the write and before the offset commit."""
    def ack(_envelope):
        print("drill: die-before-commit — the answer is written, the offset is not committed",
              file=sys.stderr)
        sys.stderr.flush()
        os._exit(3)
    source.ack = ack                                    # type: ignore[method-assign]


def main(argv: list[str] | None = None, *, source_factory=KafkaSource.from_config) -> int:
    ap = argparse.ArgumentParser(prog="python -m consumer.kafka_run",
                                 description="Consume question events from Kafka; persist answers")
    ap.add_argument("--bootstrap", default=os.environ.get("OE_KAFKA_BOOTSTRAP", "localhost:9092"))
    ap.add_argument("--topic", required=True)
    ap.add_argument("--group", default="oe-agent",
                    help="consumer group id — unique to this application; it is what tracks the offsets")
    ap.add_argument("--dlq-topic", default=None, help="default: <topic>.dlq")
    ap.add_argument("--client-config", type=Path, default=None,
                    help="JSON file of extra client settings (security); merged into consumer and producer")
    ap.add_argument("--db", type=Path, default=Path("answers.sqlite"))
    ap.add_argument("--backend", default=None, choices=BACKENDS,
                    help=f"one of {BACKENDS}; default OE_AGENT_BACKEND, then anthropic")
    ap.add_argument("--index", default=os.environ.get("OE_INDEX_DIR", "index"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--idle-exit", type=float, default=None,
                    help="stop after this many seconds without a message (default: run until Ctrl+C)")
    ap.add_argument("--drill", choices=("die-before-commit", "fail-first-ask"), default=None)
    ap.add_argument("--log-level", default="INFO",
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = ap.parse_args(argv)

    if args.max_attempts < 1:
        ap.error("--max-attempts must be >= 1")
    if args.batch_size < 1:
        ap.error("--batch-size must be >= 1")
    if args.idle_exit is not None and args.idle_exit <= 0:
        ap.error("--idle-exit must be > 0")
    if args.db.parent and not args.db.parent.exists():
        ap.error(f"--db folder {args.db.parent} does not exist")
    dlq_topic = args.dlq_topic or f"{args.topic}.dlq"
    if dlq_topic == args.topic:
        ap.error("--dlq-topic must differ from --topic")
    extra = None
    if args.client_config is not None:
        try:
            extra = json.loads(args.client_config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _config_error(f"--client-config {args.client_config}: {exc}")
        if not isinstance(extra, dict):
            _config_error(f"--client-config {args.client_config} must hold a JSON object")

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(message)s", stream=sys.stderr)

    # configuration first: agent and index, then the client and the broker
    agent = build_agent(args.backend, args.index, args.k, args.max_steps)
    try:
        source = source_factory(bootstrap=args.bootstrap, group=args.group,
                                topic=args.topic, dlq_topic=dlq_topic,
                                extra=extra, idle_exit=args.idle_exit)
    except ImportError as exc:
        _config_error(str(exc))
    broker_failed = False
    try:
        try:
            source.check()
        except KafkaSourceError as exc:
            _config_error(f"{exc} (bootstrap {args.bootstrap})")
        if args.drill == "fail-first-ask":
            agent = _FailFirstAsk(agent)
        elif args.drill == "die-before-commit":
            _die_before_commit(source)
        store = AnswerStore(args.db)
        worker = Worker(agent, source, store, max_attempts=args.max_attempts,
                        backoff=Backoff(), batch_size=args.batch_size)
        try:
            metrics = worker.run()
        except KeyboardInterrupt:
            metrics = worker.metrics
        except KafkaSourceError as exc:
            # the broker went away mid-run: say so, report what was done
            print(f"stopped: {exc}", file=sys.stderr)
            broker_failed = True
            metrics = worker.metrics
        finally:
            store.close()
    finally:
        source.close()
    out = metrics.as_dict()
    out.update({"db": str(args.db), "topic": args.topic, "group": args.group,
                "dlq_topic": dlq_topic, "committed": source.committed})
    print(json.dumps(out))
    return 1 if (metrics.dead_lettered or broker_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
