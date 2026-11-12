"""Publish a JSON Lines file of question events to a Kafka topic — the other
half of the drills.

    python -m consumer.kafka_produce --bootstrap localhost:9092 --topic oe.questions --input events.jsonl

Each non-empty line is sent as it is, so a line that is not JSON arrives as a
poison message (that is the point of one of the drills). The message key is
the event_id when the line parses — the key chooses the partition, so two
deliveries of the same event land on the same partition, in order.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def event_key(line: str) -> bytes | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("event_id"), str) and obj["event_id"].strip():
        return obj["event_id"].strip().encode("utf-8")
    return None


def publish(producer, topic: str, lines, flush_timeout: float = 10.0) -> int:
    """Produce every non-empty line; return how many the broker acknowledged."""
    failures: list = []
    sent = 0

    def on_delivery(err, _msg):
        if err is not None:
            failures.append(err)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        producer.produce(topic, value=line.encode("utf-8"), key=event_key(line),
                         on_delivery=on_delivery)
        sent += 1
    remaining = producer.flush(flush_timeout)
    if failures or remaining:
        why = failures[0] if failures else f"{remaining} message(s) not delivered in {flush_timeout}s"
        raise RuntimeError(f"produce failed: {why}")
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m consumer.kafka_produce")
    ap.add_argument("--bootstrap", default=os.environ.get("OE_KAFKA_BOOTSTRAP", "localhost:9092"))
    ap.add_argument("--topic", required=True)
    ap.add_argument("--input", type=Path, required=True,
                    help="JSON Lines file, one event per line (UTF-8; a BOM is tolerated)")
    args = ap.parse_args(argv)
    if not args.input.is_file():
        ap.error(f"--input {args.input} is not a file")
    try:
        from confluent_kafka import Producer
    except ImportError:
        print("the producer needs the confluent-kafka package: "
              "pip install -r consumer/requirements-kafka.txt", file=sys.stderr)
        return 2
    try:
        lines = args.input.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        print(f"{args.input} is not UTF-8 ({exc.reason}); re-save it as UTF-8", file=sys.stderr)
        return 2
    producer = Producer({"bootstrap.servers": args.bootstrap, "acks": "all",
                         "enable.idempotence": True})
    try:
        sent = publish(producer, args.topic, lines)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"topic": args.topic, "sent": sent}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
