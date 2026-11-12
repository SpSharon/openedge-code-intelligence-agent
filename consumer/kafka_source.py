"""KafkaSource — the EventSource protocol over a Kafka topic.

The same four calls the worker already makes, mapped onto a consumer group:

    fetch(max_events)   poll the assigned partitions. At most ONE message per
                        partition is in flight at a time: a commit in Kafka is
                        a position ("everything before this offset is done"),
                        so a second message from the same partition is not
                        handed out until the first is acked, nacked or
                        dead-lettered. A batch therefore holds at most one
                        message from each partition (the client usually hands
                        out several from one partition in a row, so in
                        practice a batch is often a single message; the
                        default --batch-size is 1).
    ack(envelope)       commit offset + 1 for that partition, synchronously —
                        called by the worker only after the SQLite write
    nack(envelope, d)   seek back to that offset (so the message is delivered
                        again) and pause the partition for d seconds — the
                        retry delay; the other partitions keep flowing
    dead_letter(e, why) produce the original bytes to the dead-letter topic
                        with the reason in the headers, wait for the broker's
                        acknowledgment, then commit past the message. If the
                        produce fails, nothing is committed, the unsent copy is
                        purged, and the message is delivered again after a
                        short pause.

Auto-commit is off. Delivery is at-least-once: a crash after the write and
before the commit means the message is delivered again on restart, and the
store's INSERT OR IGNORE on event_id absorbs it.

What Kafka does not give that MemorySource (SQS's model) does:
  * no visibility timeout — nothing is hidden; an unacked message is simply
    not committed, and is delivered again after a restart or a rebalance;
  * no receive count — the attempt number lives in this process's memory, so
    it starts again at 1 after a restart (a message that keeps failing gets
    up to max_attempts tries per process lifetime before its dead letter);
  * no dead-letter queue of its own — the dead-letter topic is written here.

Two limits to know. The consumer does not poll while the agent is answering,
so one answer that takes longer than max.poll.interval.ms (5 minutes by
default) gets this consumer removed from the group: the commit fails, the run
stops, and the message is delivered again — nothing is lost, the write being
idempotent. And a rebalance forgets a pending retry delay: the message is
simply delivered to whoever owns the partition next.

The Kafka client is injected: `KafkaSource.from_config(...)` builds the real
one from the confluent-kafka package (consumer/requirements-kafka.txt); the
tests pass fakes with the same methods, so the mapping above is tested with no
broker and without the package installed. What a fake cannot prove — that the
real client and a real broker behave as assumed — was the owner's run against
a local broker on 12 Nov 2026, recorded in the README.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable

from .source import Envelope

PARTITION_EOF_NAMES = frozenset({"_PARTITION_EOF"})
UNKNOWN_TOPIC_NAMES = frozenset({"UNKNOWN_TOPIC_OR_PART", "_UNKNOWN_TOPIC"})


class KafkaSourceError(RuntimeError):
    """A broker or client failure the caller should see, not swallow."""


def _decode(value: Any) -> str:
    """The body the worker parses. Bytes that are not UTF-8 become a stable
    placeholder (same bytes -> same string), so the worker dead-letters them
    as malformed and recognises a redelivery of the same message."""
    if value is None:
        return ""                                   # a tombstone: malformed
    if isinstance(value, str):
        return value
    try:
        return bytes(value).decode("utf-8")
    except UnicodeDecodeError:
        digest = hashlib.sha256(bytes(value)).hexdigest()[:16]
        return f"<non-UTF-8 body: {len(value)} bytes, sha256 {digest}>"


class KafkaSource:
    def __init__(self, consumer, producer, *, topic: str, dlq_topic: str,
                 topic_partition: Callable[..., Any],
                 clock: Callable[[], float] = time.monotonic,
                 poll_timeout: float = 1.0, flush_timeout: float = 10.0,
                 idle_exit: float | None = None, assigned: bool = True,
                 dlq_retry_pause: float = 5.0):
        if not topic or not dlq_topic:
            raise ValueError("topic and dlq_topic are required")
        if topic == dlq_topic:
            raise ValueError("the dead-letter topic must differ from the topic")
        self.consumer = consumer
        self.producer = producer
        self.topic = topic
        self.dlq_topic = dlq_topic
        self._tp = topic_partition
        self.clock = clock
        self.poll_timeout = poll_timeout
        self.flush_timeout = flush_timeout
        self.idle_exit = idle_exit
        self._in_flight: dict[str, tuple[str, int, int]] = {}   # receipt -> (topic, partition, offset)
        self._attempts: dict[tuple[str, int, int], int] = {}
        self._paused_until: dict[tuple[str, int], float] = {}
        self._last_activity = clock()
        # the idle clock must not run while the group is still being joined
        # (after a hard exit the old member's session has to expire first)
        self._assigned = assigned
        self.dlq_retry_pause = dlq_retry_pause
        self._fatal: KafkaSourceError | None = None
        self.dead_lettered = 0
        self.committed = 0

    # -- construction with the real client --

    @classmethod
    def from_config(cls, *, bootstrap: str, group: str, topic: str,
                    dlq_topic: str, extra: dict | None = None, **kwargs) -> "KafkaSource":
        """Build the real consumer and producer. `extra` is merged into both
        configurations — that is where security settings go (SASL, TLS); never
        on a command line."""
        try:
            from confluent_kafka import Consumer, Producer, TopicPartition
        except ImportError as exc:                  # a configuration error, not a retry
            raise ImportError(
                "the Kafka source needs the confluent-kafka package: "
                "pip install -r consumer/requirements-kafka.txt") from exc
        common = {"bootstrap.servers": bootstrap, **(extra or {})}
        consumer = Consumer({
            **common,
            "group.id": group,
            "enable.auto.commit": False,            # commit only after the write
            "auto.offset.reset": "earliest",        # a new group starts at the beginning
            "enable.partition.eof": False,
            # a member that died without leaving is noticed after this long;
            # 6 s is the broker's default minimum (the client's default is 45 s)
            "session.timeout.ms": (extra or {}).get("session.timeout.ms", 6000),
        })
        producer = Producer({**common, "acks": "all", "enable.idempotence": True})
        source = cls(consumer, producer, topic=topic, dlq_topic=dlq_topic,
                     topic_partition=TopicPartition, assigned=False, **kwargs)
        consumer.subscribe([topic], on_assign=source._on_assign,
                           on_revoke=source._on_revoke)
        return source

    def check(self, timeout: float = 5.0) -> None:
        """Reach the broker and find the topic before the first fetch; raise if
        the broker cannot be reached or the topic does not exist (a consumer
        does not create a topic by subscribing to it)."""
        try:
            metadata = self.consumer.list_topics(topic=self.topic, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — reported as configuration by the caller
            raise KafkaSourceError(f"cannot reach the Kafka broker: {exc}") from exc
        topics = getattr(metadata, "topics", None) or {}
        entry = topics.get(self.topic)
        if entry is None or getattr(entry, "error", None) is not None:
            raise KafkaSourceError(
                f"topic {self.topic!r} does not exist on the broker; create it "
                "(or produce to it) before consuming")

    # -- the four calls --

    def fetch(self, max_events: int = 10) -> list[Envelope]:
        if self._fatal is not None:
            raise self._fatal                       # a seek failed: stop, commit nothing more
        self._resume_due()
        out: list[Envelope] = []
        busy = {(t, p) for (t, p, _o) in self._in_flight.values()}
        while len(out) < max_events:
            msg = self.consumer.poll(self.poll_timeout if not out else 0)
            if msg is None:
                break
            err = msg.error()
            if err is not None:
                name = getattr(err, "name", lambda: "")()
                if name in PARTITION_EOF_NAMES:
                    continue
                if name in UNKNOWN_TOPIC_NAMES:
                    raise KafkaSourceError(
                        f"topic {self.topic!r} does not exist on the broker; "
                        "create it (or produce to it) before consuming")
                raise KafkaSourceError(f"poll failed: {err}")
            topic, partition, offset = msg.topic(), msg.partition(), msg.offset()
            if (topic, partition) in busy:
                # one in flight per partition: put this one back and stop.
                # (Every envelope of a batch is settled by the worker before
                # the next fetch, so `busy` starts empty on each call.)
                self._seek(topic, partition, offset)
                break
            busy.add((topic, partition))
            key = (topic, partition, offset)
            attempt = self._attempts.get(key, 0) + 1
            self._attempts[key] = attempt
            receipt = f"{topic}[{partition}]@{offset}#{attempt}"
            self._in_flight[receipt] = key
            raw = msg.value()
            out.append(Envelope(receipt=receipt, body=_decode(raw), attempt=attempt,
                                received_at=self.clock(),
                                meta={"topic": topic, "partition": partition,
                                      "offset": offset, "key": msg.key(), "raw": raw}))
        if out:
            self._last_activity = self.clock()
        return out

    def ack(self, envelope: Envelope) -> None:
        topic, partition, offset = self._take(envelope)
        self._commit(topic, partition, offset)

    def nack(self, envelope: Envelope, delay_seconds: float = 0.0) -> None:
        topic, partition, offset = self._take(envelope)
        self._seek(topic, partition, offset)
        if delay_seconds > 0:
            self._pause(topic, partition, delay_seconds)
        self._last_activity = self.clock()

    def dead_letter(self, envelope: Envelope, reason: str) -> None:
        topic, partition, offset = self._take(envelope)
        failure: list[Any] = []

        def on_delivery(err, _msg):
            if err is not None:
                failure.append(err)

        raw = envelope.meta.get("raw")
        try:
            self.producer.produce(
                self.dlq_topic,
                value=raw if raw is not None else b"",
                key=envelope.meta.get("key"),
                headers=[
                    ("dlq.reason", reason.encode("utf-8")),
                    ("dlq.source.topic", topic.encode("utf-8")),
                    ("dlq.source.partition", str(partition).encode("ascii")),
                    ("dlq.source.offset", str(offset).encode("ascii")),
                    ("dlq.attempts", str(envelope.attempt).encode("ascii")),
                ],
                on_delivery=on_delivery,
            )
            remaining = self.producer.flush(self.flush_timeout)
        except Exception as exc:  # noqa: BLE001 — put the message back, then report
            self._put_back_after_failed_dead_letter(topic, partition, offset)
            raise KafkaSourceError(f"dead-letter produce failed: {exc}") from exc
        if failure or remaining:
            # not acknowledged by the broker: do not commit; deliver it again
            self._put_back_after_failed_dead_letter(topic, partition, offset)
            why = failure[0] if failure else f"{remaining} message(s) not delivered in {self.flush_timeout}s"
            raise KafkaSourceError(f"dead-letter produce failed: {why}")
        self.dead_lettered += 1
        self._commit(topic, partition, offset)

    # -- the worker's idle question --

    def next_visible_in(self) -> float | None:
        """Seconds until a paused partition resumes; 0.0 while the stream may
        still deliver; None once nothing has arrived for idle_exit seconds
        (None never, when idle_exit is None — a stream has no end)."""
        now = self.clock()
        if not self._assigned:
            return 0.0                              # still joining the group
        if self._paused_until:
            return max(0.0, min(self._paused_until.values()) - now)
        if self.idle_exit is not None and not self._in_flight \
                and now - self._last_activity >= self.idle_exit:
            return None
        return 0.0

    def close(self) -> None:
        self.consumer.close()

    # -- internals --

    def _take(self, envelope: Envelope) -> tuple[str, int, int]:
        key = self._in_flight.pop(envelope.receipt, None)
        if key is None:
            raise KeyError(f"unknown or revoked receipt {envelope.receipt!r}")
        return key

    def _commit(self, topic: str, partition: int, offset: int) -> None:
        self.consumer.commit(offsets=[self._tp(topic, partition, offset + 1)],
                             asynchronous=False)
        self._attempts.pop((topic, partition, offset), None)
        self.committed += 1
        self._last_activity = self.clock()

    def _seek(self, topic: str, partition: int, offset: int) -> None:
        """Seek back so the message is delivered again. If the seek itself
        fails the position is past an unprocessed message, and the next commit
        on this partition would cover it — so the source stops: the next fetch
        raises, nothing more is committed, and a restart redelivers."""
        try:
            self.consumer.seek(self._tp(topic, partition, offset))
        except Exception as exc:  # noqa: BLE001
            self._fatal = KafkaSourceError(
                f"seek to {topic}[{partition}]@{offset} failed: {exc}; stopping so that "
                "nothing is committed past an unprocessed message")
            raise self._fatal from exc

    def _pause(self, topic: str, partition: int, seconds: float) -> None:
        self.consumer.pause([self._tp(topic, partition)])
        self._paused_until[(topic, partition)] = self.clock() + seconds

    def _put_back_after_failed_dead_letter(self, topic: str, partition: int, offset: int) -> None:
        # drop the unsent copy (it would otherwise be delivered later, a
        # duplicate dead letter), then redeliver the source message after a pause
        purge = getattr(self.producer, "purge", None)
        if callable(purge):
            try:
                purge()
                self.producer.flush(1)
            except Exception:  # noqa: BLE001 — best effort; the seek below is what matters
                pass
        self._seek(topic, partition, offset)
        if self.dlq_retry_pause > 0:
            self._pause(topic, partition, self.dlq_retry_pause)

    def _on_assign(self, _consumer, _partitions) -> None:
        self._assigned = True
        self._last_activity = self.clock()

    def _resume_due(self) -> None:
        now = self.clock()
        due = [tp for tp, at in self._paused_until.items() if at <= now]
        for topic, partition in due:
            self.consumer.resume([self._tp(topic, partition)])
            del self._paused_until[(topic, partition)]

    def _on_revoke(self, _consumer, partitions) -> None:
        """A rebalance took these partitions away: whatever was in flight on
        them belongs to another consumer now and will be delivered there."""
        gone = {(p.topic, p.partition) for p in partitions}
        for receipt, (topic, partition, _offset) in list(self._in_flight.items()):
            if (topic, partition) in gone:
                del self._in_flight[receipt]
        for tp in list(self._paused_until):
            if tp in gone:
                try:                                # do not leave it paused if it comes back
                    self.consumer.resume([self._tp(*tp)])
                except Exception:  # noqa: BLE001
                    pass
                del self._paused_until[tp]
