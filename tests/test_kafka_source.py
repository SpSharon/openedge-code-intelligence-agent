"""Offline tests for consumer/kafka_source.py — the Kafka mapping of the four calls.

No broker and no confluent-kafka package: the consumer and producer are fakes
with the client's method names (poll, seek, pause, resume, commit, produce,
flush) over an in-memory log with partitions and committed offsets per group.
What these prove is the mapping — commit after the write, one message in
flight per partition, seek-and-pause on a retry, the dead-letter produce before
the commit — and the worker's drills over it. What they cannot prove is that
the real client and a real broker behave as the fakes do: that is the run in
the README, on a machine with Docker.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import types
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from consumer import kafka_run
from consumer.kafka_produce import event_key, publish
from consumer.kafka_source import KafkaSource, KafkaSourceError
from consumer.store import AnswerStore
from consumer.worker import Backoff, Worker

TP = namedtuple("TP", "topic partition offset", defaults=(-1001,))
TOPIC, DLQ = "oe.questions", "oe.questions.dlq"

_consumer_log = logging.getLogger("consumer")
_saved_propagate = _consumer_log.propagate


def setUpModule():
    _consumer_log.propagate = False
    _consumer_log.addHandler(logging.NullHandler())


def tearDownModule():
    _consumer_log.propagate = _saved_propagate


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeError:
    def __init__(self, name: str):
        self._name = name

    def name(self) -> str:
        return self._name

    def __str__(self) -> str:
        return self._name


class FakeMsg:
    def __init__(self, topic, partition, offset, key, value, error=None):
        self._t, self._p, self._o, self._k, self._v, self._e = topic, partition, offset, key, value, error

    def topic(self): return self._t
    def partition(self): return self._p
    def offset(self): return self._o
    def key(self): return self._k
    def value(self): return self._v
    def error(self): return self._e


class FakeBroker:
    """Logs by (topic, partition) and committed offsets by group."""

    def __init__(self, partitions: int = 1):
        self.partitions = partitions
        self.logs: dict[tuple[str, int], list[tuple]] = {}
        self.committed: dict[tuple[str, str, int], int] = {}

    def append(self, topic, value, key=None, partition=0, headers=None):
        self.logs.setdefault((topic, partition), []).append((key, value, headers))

    def put(self, obj, partition=0):
        value = obj if isinstance(obj, (bytes, type(None))) else (
            obj.encode() if isinstance(obj, str) else json.dumps(obj).encode())
        key = obj["event_id"].encode() if isinstance(obj, dict) and "event_id" in obj else None
        self.append(TOPIC, value, key, partition)


class FakeConsumer:
    def __init__(self, broker: FakeBroker, group: str = "g", clock=None):
        self.broker, self.group, self.clock = broker, group, clock
        self.position = {p: broker.committed.get((group, TOPIC, p), 0)
                         for p in range(broker.partitions)}
        self.paused: set[int] = set()
        self.injected: list[FakeMsg] = []           # error messages to return first
        self.commit_fails = 0
        self.commits: list[tuple] = []
        self.seeks: list[tuple] = []
        self.closed = False
        self.topic_exists = True
        self.seek_fails = False
        self.resumed: list[int] = []
        self._next = 0

    def poll(self, timeout=None):
        if self.injected:
            return self.injected.pop(0)
        for i in range(self.broker.partitions):
            p = (self._next + i) % self.broker.partitions
            log = self.broker.logs.get((TOPIC, p), [])
            if p in self.paused or self.position[p] >= len(log):
                continue
            offset = self.position[p]
            self.position[p] += 1
            self._next = (p + 1) % self.broker.partitions
            key, value, _headers = log[offset]
            return FakeMsg(TOPIC, p, offset, key, value)
        if timeout and self.clock is not None:
            self.clock.advance(timeout)             # a blocking poll takes its timeout
        return None

    def seek(self, tp):
        if self.seek_fails:
            raise RuntimeError("Local: Erroneous state")
        self.seeks.append((tp.partition, tp.offset))
        self.position[tp.partition] = tp.offset

    def pause(self, tps):
        self.paused.update(tp.partition for tp in tps)

    def resume(self, tps):
        self.resumed.extend(tp.partition for tp in tps)
        self.paused.difference_update(tp.partition for tp in tps)

    def commit(self, offsets=None, asynchronous=True):
        assert asynchronous is False, "the commit must be synchronous"
        if self.commit_fails:
            self.commit_fails -= 1
            raise RuntimeError("commit failed: broker not available")
        for tp in offsets:
            self.broker.committed[(self.group, tp.topic, tp.partition)] = tp.offset
            self.commits.append((tp.partition, tp.offset))

    def list_topics(self, topic=None, timeout=None):
        entry = types.SimpleNamespace(error=None if self.topic_exists else FakeError("UNKNOWN_TOPIC_OR_PART"))
        return types.SimpleNamespace(topics={topic: entry})

    def close(self):
        self.closed = True


class FakeProducer:
    def __init__(self, broker: FakeBroker):
        self.broker = broker
        self.pending: list[tuple] = []
        self.fail_delivery = False
        self.never_flushes = False
        self.purged = 0

    def purge(self):
        self.purged += 1
        self.pending.clear()

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self.pending.append((topic, value, key, headers, on_delivery))

    def flush(self, timeout=None):
        if self.never_flushes:
            return len(self.pending)
        for topic, value, key, headers, cb in self.pending:
            if self.fail_delivery:
                if cb:
                    cb(FakeError("_MSG_TIMED_OUT"), None)
                continue
            self.broker.append(topic, value, key, 0, headers)
            if cb:
                cb(None, None)
        self.pending.clear()
        return 0


def make_source(broker=None, *, clock=None, idle_exit=None, group="g"):
    broker = broker or FakeBroker()
    clock = clock or FakeClock()
    consumer, producer = FakeConsumer(broker, group, clock), FakeProducer(broker)
    source = KafkaSource(consumer, producer, topic=TOPIC, dlq_topic=DLQ,
                         topic_partition=TP, clock=clock, idle_exit=idle_exit)
    source.clock = clock
    return source, consumer, producer, broker, clock


def ev(n: int) -> dict:
    return {"event_id": f"evt-{n}", "question": f"question {n}?"}


class StubAgent:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls: list[str] = []

    def ask(self, question: str) -> dict:
        self.calls.append(question)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("downstream unavailable")
        return {"answer": f"answer to: {question}",
                "citations": ["corpus/oe/oe-post.p#main"], "steps": 1}


def make_worker(case, source, clock, agent=None, max_attempts=3):
    store = AnswerStore(":memory:")
    case.addCleanup(store.close)
    worker = Worker(agent or StubAgent(), source, store, max_attempts=max_attempts,
                    backoff=Backoff(base=0.5, cap=8.0, jitter=0.25, rand=lambda: 0.0),
                    clock=clock, sleep=clock.advance)
    return worker, store


# ---------------------------------------------------------------------------
# the four calls
# ---------------------------------------------------------------------------


class TestFetch(unittest.TestCase):
    def test_envelope_carries_body_attempt_and_position(self):
        source, _c, _p, broker, _ = make_source()
        broker.put(ev(1))
        (env,) = source.fetch(10)
        self.assertEqual(json.loads(env.body), ev(1))
        self.assertEqual(env.attempt, 1)
        self.assertEqual((env.meta["topic"], env.meta["partition"], env.meta["offset"]),
                         (TOPIC, 0, 0))
        self.assertEqual(env.meta["key"], b"evt-1")
        self.assertEqual(env.receipt, f"{TOPIC}[0]@0#1")

    def test_one_message_in_flight_per_partition(self):
        source, consumer, _p, broker, _ = make_source()
        for n in range(1, 4):
            broker.put(ev(n))
        batch = source.fetch(10)
        self.assertEqual(len(batch), 1)                 # not three: a commit is a position
        self.assertEqual(consumer.position[0], 1)       # the second was put back
        self.assertEqual(source.fetch(10), [])          # still in flight: nothing more
        source.ack(batch[0])
        (nxt,) = source.fetch(10)
        self.assertEqual(nxt.meta["offset"], 1)

    def test_a_batch_holds_one_message_from_each_partition(self):
        source, _c, _p, broker, _ = make_source(FakeBroker(partitions=3))
        for p in range(3):
            broker.put(ev(p * 10 + 1), partition=p)
            broker.put(ev(p * 10 + 2), partition=p)
        batch = source.fetch(10)
        self.assertEqual(sorted(e.meta["partition"] for e in batch), [0, 1, 2])
        self.assertTrue(all(e.meta["offset"] == 0 for e in batch))

    def test_partition_eof_is_skipped_and_other_errors_are_raised(self):
        source, consumer, _p, broker, _ = make_source()
        broker.put(ev(1))
        consumer.injected.append(FakeMsg(TOPIC, 0, 0, None, None, FakeError("_PARTITION_EOF")))
        self.assertEqual(len(source.fetch(10)), 1)
        consumer.injected.append(FakeMsg(TOPIC, 0, 0, None, None, FakeError("_ALL_BROKERS_DOWN")))
        with self.assertRaises(KafkaSourceError):
            source.fetch(10)

    def test_non_utf8_and_tombstone_bodies_are_stable_and_malformed(self):
        source, _c, _p, broker, _ = make_source(FakeBroker(partitions=2))
        broker.append(TOPIC, b"\xff\xfe\x00bad", partition=0)
        broker.append(TOPIC, None, partition=1)
        bodies = {e.meta["partition"]: e for e in source.fetch(10)}
        self.assertTrue(bodies[0].body.startswith("<non-UTF-8 body: 6 bytes, sha256 "))
        self.assertEqual(bodies[0].meta["raw"], b"\xff\xfe\x00bad")
        self.assertEqual(bodies[1].body, "")


class TestAckNack(unittest.TestCase):
    def test_ack_commits_offset_plus_one_synchronously(self):
        source, consumer, _p, broker, _ = make_source()
        broker.put(ev(1))
        (env,) = source.fetch(10)
        source.ack(env)
        self.assertEqual(consumer.commits, [(0, 1)])
        self.assertEqual(broker.committed[("g", TOPIC, 0)], 1)
        with self.assertRaises(KeyError):               # a receipt is good once
            source.ack(env)

    def test_nack_seeks_back_and_the_message_returns_as_attempt_two(self):
        source, consumer, _p, broker, _ = make_source()
        broker.put(ev(1))
        (env,) = source.fetch(10)
        source.nack(env)
        self.assertEqual(consumer.seeks[-1], (0, 0))
        self.assertEqual(consumer.commits, [])          # nothing committed
        (again,) = source.fetch(10)
        self.assertEqual((again.meta["offset"], again.attempt), (0, 2))

    def test_nack_with_a_delay_pauses_only_that_partition(self):
        clock = FakeClock()
        source, consumer, _p, broker, _ = make_source(FakeBroker(partitions=2), clock=clock)
        broker.put(ev(1), partition=0)
        broker.put(ev(2), partition=1)
        batch = {e.meta["partition"]: e for e in source.fetch(10)}
        source.nack(batch[0], 2.0)
        source.ack(batch[1])
        self.assertEqual(consumer.paused, {0})
        self.assertAlmostEqual(source.next_visible_in(), 2.0)
        self.assertEqual(source.fetch(10), [])          # partition 0 is waiting
        clock.advance(2.0)
        (again,) = source.fetch(10)
        self.assertEqual((again.meta["partition"], again.attempt), (0, 2))
        self.assertEqual(consumer.paused, set())

    def test_a_revoked_partition_drops_its_in_flight_message(self):
        source, _c, _p, broker, _ = make_source()
        broker.put(ev(1))
        (env,) = source.fetch(10)
        source._on_revoke(None, [TP(TOPIC, 0)])
        with self.assertRaises(KeyError):
            source.ack(env)


class TestDeadLetter(unittest.TestCase):
    def test_dead_letter_produces_the_original_bytes_with_the_reason_then_commits(self):
        source, consumer, _p, broker, _ = make_source()
        broker.put("not json")
        (env,) = source.fetch(10)
        source.dead_letter(env, "malformed: body is not JSON")
        (key, value, headers) = broker.logs[(DLQ, 0)][0]
        self.assertEqual(value, b"not json")
        h = dict(headers)
        self.assertEqual(h["dlq.reason"], b"malformed: body is not JSON")
        self.assertEqual((h["dlq.source.topic"], h["dlq.source.partition"],
                          h["dlq.source.offset"], h["dlq.attempts"]),
                         (TOPIC.encode(), b"0", b"0", b"1"))
        self.assertEqual(consumer.commits, [(0, 1)])

    def test_a_failed_dead_letter_produce_commits_nothing_and_redelivers(self):
        for mode in ("fail_delivery", "never_flushes"):
            with self.subTest(mode=mode):
                source, consumer, producer, broker, _ = make_source()
                broker.put("not json")
                setattr(producer, mode, True)
                (env,) = source.fetch(10)
                with self.assertRaises(KafkaSourceError):
                    source.dead_letter(env, "malformed")
                self.assertEqual(consumer.commits, [])
                self.assertNotIn((DLQ, 0), broker.logs)
                self.assertEqual(producer.purged, 1)    # the unsent copy is dropped
                self.assertEqual(producer.pending, [])
                self.assertEqual(source.fetch(10), [])  # paused: no hot loop on a broken topic
                source.clock.advance(5.0)
                (again,) = source.fetch(10)             # then delivered again
                self.assertEqual((again.meta["offset"], again.attempt), (0, 2))


class TestIdle(unittest.TestCase):
    def test_a_stream_has_no_end_unless_idle_exit_is_set(self):
        clock = FakeClock()
        source, *_ = make_source(clock=clock)
        clock.advance(3600)
        self.assertEqual(source.next_visible_in(), 0.0)

    def test_idle_exit_counts_from_the_last_activity(self):
        clock = FakeClock()
        source, _c, _p, broker, _ = make_source(clock=clock, idle_exit=10.0)
        broker.put(ev(1))
        clock.advance(9.0)
        (env,) = source.fetch(10)                       # activity
        clock.advance(9.0)
        self.assertEqual(source.next_visible_in(), 0.0)  # in flight: never idle
        source.ack(env)
        clock.advance(9.9)
        self.assertEqual(source.next_visible_in(), 0.0)
        clock.advance(0.2)
        self.assertIsNone(source.next_visible_in())

    def test_topic_and_dead_letter_topic_must_differ(self):
        with self.assertRaises(ValueError):
            KafkaSource(object(), object(), topic="a", dlq_topic="a", topic_partition=TP)


class TestSafety(unittest.TestCase):
    def test_a_failed_seek_stops_the_source_so_nothing_is_committed_past_the_message(self):
        source, consumer, _p, broker, _ = make_source()
        broker.put(ev(1)); broker.put(ev(2))
        (env,) = source.fetch(10)
        consumer.seek_fails = True
        with self.assertRaises(KafkaSourceError):
            source.nack(env)
        consumer.seek_fails = False
        with self.assertRaises(KafkaSourceError):       # the next fetch refuses to go on
            source.fetch(10)
        self.assertEqual(consumer.commits, [])

    def test_idle_exit_does_not_count_while_the_group_is_still_being_joined(self):
        clock = FakeClock()
        broker = FakeBroker()
        consumer, producer = FakeConsumer(broker, "g", clock), FakeProducer(broker)
        source = KafkaSource(consumer, producer, topic=TOPIC, dlq_topic=DLQ,
                             topic_partition=TP, clock=clock, idle_exit=10.0, assigned=False)
        clock.advance(60.0)                             # a dead member's session expiring
        self.assertEqual(source.next_visible_in(), 0.0)
        source._on_assign(None, [TP(TOPIC, 0)])
        clock.advance(9.0)
        self.assertEqual(source.next_visible_in(), 0.0)
        clock.advance(1.5)
        self.assertIsNone(source.next_visible_in())

    def test_a_revoked_paused_partition_is_resumed_and_forgotten(self):
        clock = FakeClock()
        source, consumer, _p, broker, _ = make_source(clock=clock)
        broker.put(ev(1))
        (env,) = source.fetch(10)
        source.nack(env, 30.0)
        source._on_revoke(None, [TP(TOPIC, 0)])
        self.assertEqual(consumer.resumed, [0])
        self.assertEqual(source.next_visible_in(), 0.0)

    def test_check_names_a_missing_topic(self):
        source, consumer, *_ = make_source()
        source.check()                                  # exists: no error
        consumer.topic_exists = False
        with self.assertRaisesRegex(KafkaSourceError, "does not exist"):
            source.check()

    def test_unknown_topic_from_poll_is_named(self):
        source, consumer, *_ = make_source()
        consumer.injected.append(FakeMsg(TOPIC, 0, 0, None, None, FakeError("UNKNOWN_TOPIC_OR_PART")))
        with self.assertRaisesRegex(KafkaSourceError, "does not exist"):
            source.fetch(10)


class TestFromConfig(unittest.TestCase):
    """from_config against a stand-in confluent_kafka module: the settings that
    carry the guarantees, and the callbacks handed to subscribe."""

    def test_settings_and_subscription(self):
        made = {}

        class C:
            def __init__(self, conf): made["consumer"] = conf
            def subscribe(self, topics, on_assign=None, on_revoke=None):
                made["subscribe"] = (topics, on_assign, on_revoke)

        class P:
            def __init__(self, conf): made["producer"] = conf

        fake = types.SimpleNamespace(Consumer=C, Producer=P, TopicPartition=TP)
        with mock.patch.dict(sys.modules, {"confluent_kafka": fake}):
            source = KafkaSource.from_config(bootstrap="b:9092", group="grp", topic=TOPIC,
                                             dlq_topic=DLQ, extra={"security.protocol": "SSL"},
                                             idle_exit=5.0)
        c, p = made["consumer"], made["producer"]
        self.assertIs(c["enable.auto.commit"], False)
        self.assertEqual((c["group.id"], c["auto.offset.reset"], c["bootstrap.servers"]),
                         ("grp", "earliest", "b:9092"))
        self.assertEqual(c["security.protocol"], "SSL")
        self.assertEqual((p["acks"], p["enable.idempotence"], p["security.protocol"]),
                         ("all", True, "SSL"))
        topics, on_assign, on_revoke = made["subscribe"]
        self.assertEqual(topics, [TOPIC])
        self.assertEqual((on_assign, on_revoke), (source._on_assign, source._on_revoke))
        self.assertEqual(source.next_visible_in(), 0.0)  # not assigned yet: never idle


class TestDrillsAndStops(unittest.TestCase):
    def test_die_before_commit_exits_after_the_write_and_before_the_commit(self):
        clock = FakeClock()
        source, consumer, _p, broker, _ = make_source(clock=clock)
        broker.put(ev(1))
        store = AnswerStore(":memory:")
        self.addCleanup(store.close)
        kafka_run._die_before_commit(source)
        worker = Worker(StubAgent(), source, store, clock=clock, sleep=clock.advance)

        class Exited(BaseException):
            pass

        def fake_exit(code):
            raise Exited(code)

        with mock.patch.object(kafka_run.os, "_exit", fake_exit), \
                redirect_stderr(io.StringIO()), self.assertRaises(Exited) as ctx:
            worker.run_once()
        self.assertEqual(ctx.exception.args, (3,))
        self.assertEqual(len(store.answers()), 1)       # written
        self.assertEqual(consumer.commits, [])          # not committed

    def test_a_broker_failure_mid_run_is_reported_and_exits_1(self):
        def factory(**kw):
            source, consumer, _p, broker, _ = make_source(idle_exit=kw["idle_exit"])
            consumer.injected.append(FakeMsg(TOPIC, 0, 0, None, None, FakeError("_ALL_BROKERS_DOWN")))
            return source

        out, err = io.StringIO(), io.StringIO()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(kafka_run, "build_agent", lambda *a, **k: StubAgent()), \
                redirect_stdout(out), redirect_stderr(err):
            code = kafka_run.main(["--topic", TOPIC, "--db", str(Path(tmp.name) / "a.sqlite"),
                                   "--backend", "fake", "--idle-exit", "5"], source_factory=factory)
        self.assertEqual(code, 1)
        self.assertIn("stopped: poll failed", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["processed"], 0)


# ---------------------------------------------------------------------------
# the worker's drills, over the Kafka mapping
# ---------------------------------------------------------------------------


class TestWorkerOverKafka(unittest.TestCase):
    def test_mixed_batch_two_processed_one_dead_letter_one_duplicate(self):
        clock = FakeClock()
        source, consumer, _p, broker, _ = make_source(clock=clock, idle_exit=5.0)
        broker.put(ev(1)); broker.put(ev(2)); broker.put("not json"); broker.put(ev(1))
        worker, store = make_worker(self, source, clock)
        m = worker.run()
        self.assertEqual((m.processed, m.duplicates, m.dead_lettered, m.malformed),
                         (2, 1, 1, 1))
        self.assertEqual(len(store.answers()), 2)
        self.assertEqual(broker.committed[("g", TOPIC, 0)], 4)      # all four are behind us
        self.assertEqual(broker.logs[(DLQ, 0)][0][1], b"not json")
        self.assertEqual(len(worker.agent.calls), 2)                # the duplicate cost no model call

    def test_crash_after_the_write_before_the_commit_is_absorbed_on_restart(self):
        clock = FakeClock()
        source, consumer, _p, broker, _ = make_source(clock=clock, idle_exit=5.0)
        broker.put(ev(1))
        agent = StubAgent()
        store = AnswerStore(":memory:")
        self.addCleanup(store.close)
        consumer.commit_fails = 1                       # the write lands; the commit does not
        w1 = Worker(agent, source, store, clock=clock, sleep=clock.advance)
        w1.run_once()
        self.assertEqual(len(store.answers()), 1)
        self.assertEqual(w1.metrics.source_errors, 1)
        self.assertNotIn(("g", TOPIC, 0), broker.committed)
        # "restart": a new consumer in the same group starts from the committed offset
        source2, consumer2, _p2, _b, _ = make_source(broker, clock=clock, idle_exit=5.0)
        w2 = Worker(agent, source2, store, clock=clock, sleep=clock.advance)
        m = w2.run()
        self.assertEqual((m.processed, m.duplicates), (0, 1))
        self.assertEqual(len(store.answers()), 1)                  # still one row
        self.assertEqual(len(agent.calls), 1)                       # and one model call
        self.assertEqual(broker.committed[("g", TOPIC, 0)], 1)      # now committed

    def test_transient_failure_is_retried_after_the_backoff_then_processed(self):
        clock = FakeClock()
        source, consumer, _p, broker, _ = make_source(clock=clock, idle_exit=5.0)
        broker.put(ev(1)); broker.put(ev(2))
        worker, store = make_worker(self, source, clock, agent=StubAgent(fail_times=1))
        t0 = clock()
        m = worker.run()
        self.assertEqual((m.processed, m.retried, m.dead_lettered), (2, 1, 0))
        self.assertGreaterEqual(clock() - t0, 0.5)                  # the 0.5 s backoff was waited
        self.assertEqual([r["event_id"] for r in store.answers()], ["evt-1", "evt-2"])
        self.assertEqual(store.answers()[0]["attempt"], 2)

    def test_exhausted_retries_end_on_the_dead_letter_topic_and_the_next_event_flows(self):
        clock = FakeClock()
        source, consumer, _p, broker, _ = make_source(clock=clock, idle_exit=5.0)
        broker.put(ev(1)); broker.put(ev(2))
        worker, store = make_worker(self, source, clock, agent=StubAgent(fail_times=3))
        m = worker.run()
        self.assertEqual((m.processed, m.retried, m.dead_lettered), (1, 2, 1))
        (key, value, headers) = broker.logs[(DLQ, 0)][0]
        self.assertEqual(json.loads(value), ev(1))
        self.assertIn(b"exhausted after 3 attempts", dict(headers)["dlq.reason"])
        self.assertEqual([r["event_id"] for r in store.answers()], ["evt-2"])

    def test_order_is_kept_within_a_partition(self):
        clock = FakeClock()
        source, *_rest = make_source(clock=clock, idle_exit=5.0)
        broker = _rest[2]
        for n in range(1, 6):
            broker.put(ev(n))
        worker, store = make_worker(self, source, clock)
        worker.run()
        self.assertEqual([r["event_id"] for r in store.answers()],
                         [f"evt-{n}" for n in range(1, 6)])


# ---------------------------------------------------------------------------
# the producer and the command line
# ---------------------------------------------------------------------------


class TestProducer(unittest.TestCase):
    def test_key_is_the_event_id_and_poison_lines_are_sent_as_they_are(self):
        broker = FakeBroker()
        producer = FakeProducer(broker)
        sent = publish(producer, TOPIC, [json.dumps(ev(1)), "", "not json", '{"event_id": 7}'])
        self.assertEqual(sent, 3)
        log = broker.logs[(TOPIC, 0)]
        self.assertEqual([k for k, _v, _h in log], [b"evt-1", None, None])
        self.assertEqual(log[1][1], b"not json")
        self.assertIsNone(event_key("[1, 2]"))

    def test_an_unacknowledged_produce_is_an_error(self):
        producer = FakeProducer(FakeBroker())
        producer.fail_delivery = True
        with self.assertRaises(RuntimeError):
            publish(producer, TOPIC, [json.dumps(ev(1))])


class TestKafkaRunConfiguration(unittest.TestCase):
    """Configuration stops the run with exit 2 before anything is polled."""

    def _run(self, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = kafka_run.main(argv, **kwargs)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "a.sqlite")
        patcher = mock.patch.object(kafka_run, "build_agent", lambda *a, **k: StubAgent())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_package_is_exit_2_with_the_install_line(self):
        with mock.patch.dict(sys.modules, {"confluent_kafka": None}):
            code, _out, err = self._run(["--topic", TOPIC, "--db", self.db, "--backend", "fake"])
        self.assertEqual(code, 2)
        self.assertIn("pip install -r consumer/requirements-kafka.txt", err)

    def test_unreachable_broker_is_exit_2_and_the_consumer_is_closed(self):
        made = {}

        def factory(**kw):
            source, consumer, *_ = make_source(idle_exit=kw["idle_exit"])
            consumer.list_topics = mock.Mock(side_effect=RuntimeError("timed out"))
            made["consumer"] = consumer
            return source

        code, _out, err = self._run(["--topic", TOPIC, "--db", self.db, "--backend", "fake"],
                                    source_factory=factory)
        self.assertEqual(code, 2)
        self.assertIn("cannot reach the Kafka broker", err)
        self.assertTrue(made["consumer"].closed)

    def test_a_run_prints_metrics_and_exits_1_when_something_was_dead_lettered(self):
        clock = FakeClock()

        def factory(**kw):
            self.assertEqual(kw["dlq_topic"], DLQ)                  # the default name
            source, _c, _p, broker, _ = make_source(clock=clock, idle_exit=kw["idle_exit"])
            broker.put(ev(1)); broker.put("not json")
            return source

        with mock.patch.object(kafka_run.Worker, "__init__",
                               _worker_init_with(clock)):
            code, out, _err = self._run(["--topic", TOPIC, "--db", self.db, "--backend", "fake",
                                         "--idle-exit", "5"], source_factory=factory)
        metrics = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual((metrics["processed"], metrics["dead_lettered"], metrics["committed"]),
                         (1, 1, 2))
        self.assertEqual((metrics["topic"], metrics["dlq_topic"], metrics["group"]),
                         (TOPIC, DLQ, "oe-agent"))

    def test_fail_first_ask_drill_retries_once(self):
        agent = kafka_run._FailFirstAsk(StubAgent())
        with self.assertRaises(ConnectionError):
            agent.ask("q")
        self.assertEqual(agent.ask("q")["answer"], "answer to: q")


def _worker_init_with(clock):
    """Worker.__init__ with the fake clock and sleep, for the CLI test."""
    real = Worker.__init__

    def init(self, agent, source, store, **kw):
        kw.update(clock=clock, sleep=clock.advance)
        real(self, agent, source, store, **kw)
    return init


if __name__ == "__main__":
    unittest.main()
