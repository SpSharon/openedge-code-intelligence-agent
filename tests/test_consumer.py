"""Offline tests for consumer/ — the drills an integration worker must pass.

No broker, no network, no key, no sleeping: MemorySource takes a fake clock,
the Worker a fake sleep, and the agent is a stub except in the two
integration tests at the end (ToolAgent + FakeLLM over the local index,
as in test_agent3).

The drills, each its own test:
  duplicate delivery            -> one row
  crash after write, before ack -> redelivered, acked as duplicate, one row
  downstream failure            -> retries with growing backoff, then dead letter
  transient failure then success-> one row, one retry
  poison message                -> dead-lettered at once, the next event processed
  ordering                      -> answers in arrival order
  log lines                     -> JSON, with event id, correlation id, attempt
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from consumer.events import MalformedEvent, parse_event
from consumer.source import FileSource, MemorySource
from consumer.store import AnswerStore
from consumer.worker import Backoff, Worker, is_permanent

GOOD = {"event_id": "evt-1", "question": "what calls ar-invoice.p?",
        "correlation_id": "req-77"}

_consumer_log = logging.getLogger("consumer")
_saved_propagate = _consumer_log.propagate


def setUpModule():
    # keep the drills' log lines out of the test output; assertLogs attaches
    # its own handler to the "consumer" logger, so it still sees them
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


class StubAgent:
    """ask() answers from a script; `fail_times` raises first, then answers."""

    def __init__(self, fail_times: int = 0, exc: Exception | None = None):
        self.fail_times = fail_times
        self.exc = exc or ConnectionError("downstream unavailable")
        self.calls: list[str] = []

    def ask(self, question: str) -> dict:
        self.calls.append(question)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        return {"answer": f"answer to: {question}",
                "citations": ["corpus/oe/oe-post.p#main"], "steps": 1}


def make_worker(agent=None, *, clock=None, max_attempts=3,
                visibility=30.0, max_receive_count=None, rand=lambda: 0.0):
    clock = clock or FakeClock()
    source = MemorySource(visibility_timeout=visibility,
                          max_receive_count=max_receive_count, clock=clock)
    store = AnswerStore(":memory:")
    worker = Worker(agent or StubAgent(), source, store,
                    max_attempts=max_attempts,
                    backoff=Backoff(base=0.5, cap=8.0, jitter=0.25, rand=rand),
                    clock=clock, sleep=clock.advance)
    return worker, source, store, clock


# ---------------------------------------------------------------------------
# the message contract
# ---------------------------------------------------------------------------


class TestParseEvent(unittest.TestCase):
    def test_minimal_event_defaults_correlation_to_event_id(self):
        ev = parse_event({"event_id": "e1", "question": "q"})
        self.assertEqual(ev.correlation_id, "e1")
        self.assertEqual(ev.attributes, {})

    def test_json_string_body(self):
        ev = parse_event(json.dumps(GOOD))
        self.assertEqual(ev.event_id, "evt-1")
        self.assertEqual(ev.correlation_id, "req-77")

    def test_malformed_bodies(self):
        for bad in ("not json", "[1,2]", {"event_id": "", "question": "q"},
                    {"event_id": "e", "question": "   "}, {"question": "q"},
                    {"event_id": 7, "question": "q"},
                    {"event_id": "e", "question": "q", "correlation_id": ""},
                    {"event_id": "e", "question": "q", "attributes": "x"}):
            with self.assertRaises(MalformedEvent, msg=repr(bad)):
                parse_event(bad)


# ---------------------------------------------------------------------------
# the source: queue semantics without a queue
# ---------------------------------------------------------------------------


class TestMemorySource(unittest.TestCase):
    def test_fetch_hides_until_visibility_timeout_then_redelivers(self):
        clock = FakeClock()
        src = MemorySource(visibility_timeout=30, clock=clock)
        src.put(GOOD)
        first = src.fetch()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].attempt, 1)
        self.assertEqual(src.fetch(), [])            # hidden while in flight
        clock.advance(31)
        again = src.fetch()
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].attempt, 2)        # redelivered, attempt + 1
        self.assertNotEqual(again[0].receipt, first[0].receipt)
        with self.assertRaises(KeyError):
            src.ack(first[0])                        # the old receipt expired

    def test_ack_removes_and_nack_delays(self):
        clock = FakeClock()
        src = MemorySource(visibility_timeout=30, clock=clock)
        src.put(GOOD)
        env = src.fetch()[0]
        src.nack(env, delay_seconds=5)
        self.assertEqual(src.fetch(), [])
        self.assertAlmostEqual(src.next_visible_in(), 5.0)
        clock.advance(5)
        env2 = src.fetch()[0]
        src.ack(env2)
        self.assertEqual(src.pending(), 0)
        self.assertIsNone(src.next_visible_in())

    def test_receive_count_redrive_dead_letters_by_itself(self):
        clock = FakeClock()
        src = MemorySource(visibility_timeout=1, max_receive_count=2, clock=clock)
        src.put(GOOD)
        src.fetch(); clock.advance(2)                # attempt 1, never acked
        src.fetch(); clock.advance(2)                # attempt 2, never acked
        self.assertEqual(src.fetch(), [])            # 3rd would exceed: redriven
        self.assertEqual(len(src.dead_letters), 1)
        self.assertEqual(src.dead_letters[0]["reason"], "max_receive_count exceeded")
        self.assertEqual(src.dead_letters[0]["attempts"], 2)   # delivered twice, exactly
        self.assertEqual(src.pending(), 0)

    def test_retried_message_returns_behind_what_was_visible(self):
        clock = FakeClock()
        src = MemorySource(visibility_timeout=30, clock=clock)
        for i in range(4):
            src.put({"event_id": f"e{i}", "question": f"q{i}"})
        first = src.fetch(1)[0]                      # e0 in flight
        src.nack(first, delay_seconds=1)             # e0 back in 1 s
        clock.advance(1)
        order = [env.body["event_id"] for env in src.fetch(10)]
        self.assertEqual(order, ["e1", "e2", "e3", "e0"])   # e0 behind what was visible


# ---------------------------------------------------------------------------
# the drills
# ---------------------------------------------------------------------------


class TestWorkerDrills(unittest.TestCase):
    def test_duplicate_delivery_writes_once(self):
        worker, source, store, _ = make_worker()
        source.put(GOOD)
        source.put(dict(GOOD))                        # the same event, delivered twice
        worker.run()
        self.assertEqual(store.counts(), {"answers": 1, "dead_letters": 0})
        self.assertEqual(worker.metrics.processed, 1)
        self.assertEqual(worker.metrics.duplicates, 1)
        self.assertEqual(len(worker.agent.calls), 1)  # the model was asked once

    def test_crash_after_write_before_ack_is_safe(self):
        worker, source, store, clock = make_worker(visibility=30)
        source.put(GOOD)
        real_ack = source.ack
        state = {"crashed": False}

        def ack_that_dies_once(envelope):
            if not state["crashed"]:
                state["crashed"] = True
                raise RuntimeError("process killed between write and ack")
            return real_ack(envelope)

        source.ack = ack_that_dies_once
        worker.run_once()                             # the write happened, the ack did not
        self.assertEqual(worker.metrics.source_errors, 1)
        self.assertEqual(store.counts()["answers"], 1)
        clock.advance(31)                             # visibility timeout: redelivered
        worker.run()
        self.assertEqual(store.counts()["answers"], 1)  # still one row
        self.assertEqual(worker.metrics.duplicates, 1)
        self.assertEqual(len(worker.agent.calls), 1)   # not asked again
        self.assertEqual(source.pending(), 0)

    def test_downstream_failure_retries_with_backoff_then_dead_letters(self):
        agent = StubAgent(fail_times=99)              # never recovers
        worker, source, store, clock = make_worker(agent, max_attempts=3)
        source.put(GOOD)
        delays = []
        real_nack = source.nack

        def spy_nack(envelope, delay_seconds=0.0):
            delays.append(delay_seconds)
            return real_nack(envelope, delay_seconds)

        source.nack = spy_nack
        worker.run()
        self.assertEqual(delays, [0.5, 1.0])          # base * 2**(attempt-1), no jitter
        self.assertEqual(len(agent.calls), 3)         # exactly max_attempts model calls
        self.assertEqual(worker.metrics.retried, 2)
        self.assertEqual(worker.metrics.dead_lettered, 1)
        self.assertEqual(store.counts(), {"answers": 0, "dead_letters": 1})
        dl = store.dead_letters()[0]
        self.assertEqual(dl["event_id"], "evt-1")
        self.assertEqual(dl["attempts"], 3)
        self.assertIn("exhausted after 3 attempts", dl["reason"])
        self.assertIn("ConnectionError", dl["reason"])
        self.assertEqual(len(source.dead_letters), 1)
        self.assertEqual(source.pending(), 0)

    def test_transient_failure_then_success(self):
        agent = StubAgent(fail_times=1)
        worker, source, store, _ = make_worker(agent)
        source.put(GOOD)
        worker.run()
        self.assertEqual(worker.metrics.retried, 1)
        self.assertEqual(worker.metrics.processed, 1)
        row = store.answer("evt-1")
        self.assertEqual(row["attempt"], 2)           # the attempt that succeeded
        self.assertEqual(row["correlation_id"], "req-77")
        self.assertEqual(row["citations"], ["corpus/oe/oe-post.p#main"])

    def test_poison_message_dead_lettered_and_next_processed(self):
        worker, source, store, _ = make_worker()
        source.put("this is not an event")
        source.put({"event_id": "evt-2", "question": "what does oe-post.p do?"})
        worker.run()
        self.assertEqual(store.counts(), {"answers": 1, "dead_letters": 1})
        self.assertEqual(worker.metrics.malformed, 1)
        self.assertEqual(worker.metrics.retried, 0)   # never retried
        self.assertIsNotNone(store.answer("evt-2"))
        dl = store.dead_letters()[0]
        self.assertIsNone(dl["event_id"])
        self.assertTrue(dl["reason"].startswith("malformed:"))

    def test_ordering_preserved(self):
        worker, source, store, _ = make_worker()
        for i in range(5):
            source.put({"event_id": f"evt-{i}", "question": f"q{i}"})
        worker.run()
        self.assertEqual([r["event_id"] for r in store.answers()],
                         [f"evt-{i}" for i in range(5)])
        self.assertEqual(worker.agent.calls, [f"q{i}" for i in range(5)])

    def test_log_lines_are_json_with_ids(self):
        agent = StubAgent(fail_times=1)
        worker, source, store, _ = make_worker(agent)
        source.put(GOOD)
        source.put("poison")
        with self.assertLogs("consumer", level="INFO") as captured:
            worker.run()
        records = [json.loads(r.getMessage()) for r in captured.records]
        events = [r["event"] for r in records]
        # the retried event comes back after its delay, behind what is visible
        self.assertEqual(events, ["retry", "dead_letter", "processed"])
        retry, dead, processed = records
        self.assertEqual(retry["event_id"], "evt-1")
        self.assertEqual(retry["correlation_id"], "req-77")
        self.assertEqual(retry["attempt"], 1)
        self.assertIn("retry in", retry["reason"])
        self.assertEqual(processed["attempt"], 2)
        self.assertIn("duration_ms", processed)
        self.assertIsNone(dead["event_id"])
        self.assertTrue(dead["reason"].startswith("malformed:"))

    def test_metrics_counts(self):
        agent = StubAgent(fail_times=1)
        worker, source, store, _ = make_worker(agent)
        source.put(GOOD)
        source.put(dict(GOOD))
        source.put("poison")
        m = worker.run().as_dict()
        self.assertEqual((m["processed"], m["duplicates"], m["retried"],
                          m["dead_lettered"], m["malformed"]), (1, 1, 1, 1, 1))
        self.assertEqual(m["fetched"], 4)             # 3 + the one redelivery

    def test_run_sleeps_only_for_delayed_retries(self):
        agent = StubAgent(fail_times=1)
        worker, source, store, clock = make_worker(agent)
        source.put(GOOD)
        slept = []
        worker.sleep = lambda s: (slept.append(s), clock.advance(s))
        worker.run()
        self.assertEqual(slept, [0.5])                # exactly the one backoff delay
        self.assertEqual(worker.metrics.processed, 1)


class TestWorkerMore(unittest.TestCase):
    def test_refusal_is_flagged_through_the_real_contract(self):
        class RefusingAgent:
            def ask(self, question):
                return {"answer": "That is **not** in the retrieved code.",
                        "citations": [], "steps": 1}
        worker, source, store, _ = make_worker(RefusingAgent())
        source.put(GOOD)
        worker.run()
        self.assertEqual(store.answer("evt-1")["refused"], 1)

    def test_permanent_error_stops_the_run_instead_of_retrying(self):
        agent = StubAgent(fail_times=99, exc=ImportError("No module named 'anthropic'"))
        worker, source, store, _ = make_worker(agent)
        source.put(GOOD)
        source.put({"event_id": "evt-2", "question": "q2"})
        with self.assertRaises(ImportError):
            worker.run()
        self.assertEqual(len(agent.calls), 1)         # not retried, not the next event
        self.assertEqual(store.counts(), {"answers": 0, "dead_letters": 0})
        self.assertEqual(source.pending(), 2)         # both still in the queue

    def test_source_failure_is_logged_not_fatal(self):
        agent = StubAgent(fail_times=1)
        worker, source, store, clock = make_worker(agent, visibility=5)
        source.put(GOOD)

        def nack_that_fails(envelope, delay_seconds=0.0):
            raise KeyError("ReceiptHandleIsInvalid")

        source.nack = nack_that_fails
        with self.assertLogs("consumer", level="ERROR") as captured:
            worker.run_once()                         # ask fails -> nack fails -> logged
        self.assertEqual(worker.metrics.source_errors, 1)
        self.assertIn("source_error", captured.records[0].getMessage())
        clock.advance(6)                              # visibility timeout: redelivered
        worker.run()
        self.assertEqual(store.counts()["answers"], 1)

    def test_race_inserted_elsewhere_counts_as_duplicate(self):
        worker, source, store, _ = make_worker()
        source.put(GOOD)
        real_has = store.has
        store.has = lambda event_id: False            # the check says "new"...
        ev = parse_event(GOOD)
        store.insert_answer(ev, {"answer": "from another worker", "citations": []},
                            attempt=1, duration_ms=1)   # ...but another worker wrote first
        worker.run()
        store.has = real_has
        self.assertEqual(store.counts()["answers"], 1)
        self.assertEqual(store.answer("evt-1")["answer"], "from another worker")
        self.assertEqual(worker.metrics.duplicates, 1)
        self.assertEqual(worker.metrics.processed, 0)

    def test_run_max_batches_stops_early(self):
        worker, source, store, _ = make_worker()
        for i in range(3):
            source.put({"event_id": f"evt-{i}", "question": f"q{i}"})
        worker.batch_size = 1
        worker.run(max_batches=2)
        self.assertEqual(store.counts()["answers"], 2)
        self.assertEqual(source.pending(), 1)

    def test_source_redrive_is_recorded_in_store_and_metrics(self):
        clock = FakeClock()
        source = MemorySource(visibility_timeout=1, max_receive_count=2, clock=clock)
        store = AnswerStore(":memory:")

        class Hanger:                                 # never returns a result
            def ask(self, question):
                raise TimeoutError("model call timed out")

        worker = Worker(Hanger(), source, store, max_attempts=99,
                        backoff=Backoff(base=0.0, jitter=0.0, rand=lambda: 0.0),
                        clock=clock, sleep=clock.advance)
        source.put(GOOD)
        worker.run()
        self.assertEqual(worker.metrics.redriven, 1)
        dl = store.dead_letters()[0]
        self.assertEqual((dl["event_id"], dl["receipt"], dl["attempts"]),
                         ("evt-1", "redrive", 2))
        self.assertEqual(dl["reason"], "max_receive_count exceeded")

    def test_failed_dead_letter_call_on_poison_is_absorbed_on_redelivery(self):
        worker, source, store, clock = make_worker(visibility=5, max_receive_count=4)
        source.put("poison")
        real_dl = source.dead_letter
        calls = {"n": 0}

        def dead_letter_fails_once(envelope, reason):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("ReceiptHandleIsInvalid")
            return real_dl(envelope, reason)

        source.dead_letter = dead_letter_fails_once
        worker.run()                                  # first attempt: row written, call fails
        clock.advance(6)                              # redelivered after the timeout
        worker.run()
        self.assertEqual(store.counts(), {"answers": 0, "dead_letters": 1})   # one row
        self.assertEqual(worker.metrics.malformed, 1)
        self.assertEqual(worker.metrics.dead_lettered, 1)
        self.assertEqual(worker.metrics.duplicates, 1)
        self.assertEqual(source.pending(), 0)

    def test_failed_dead_letter_call_on_exhausted_retries_makes_no_extra_model_call(self):
        agent = StubAgent(fail_times=99)
        worker, source, store, clock = make_worker(agent, max_attempts=3,
                                                   visibility=5, max_receive_count=4)
        source.put(GOOD)
        real_dl = source.dead_letter
        calls = {"n": 0}

        def dead_letter_fails_once(envelope, reason):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("ReceiptHandleIsInvalid")
            return real_dl(envelope, reason)

        source.dead_letter = dead_letter_fails_once
        worker.run()                                  # 3 attempts, row written, call fails
        clock.advance(6)
        worker.run()                                  # redelivered: no 4th model call
        self.assertEqual(len(agent.calls), 3)
        self.assertEqual(store.counts(), {"answers": 0, "dead_letters": 1})
        self.assertEqual(worker.metrics.dead_lettered, 1)
        self.assertEqual(worker.metrics.duplicates, 1)
        self.assertEqual(source.pending(), 0)

    def test_auth_and_not_found_errors_are_permanent(self):
        class ApiError(Exception):
            def __init__(self, status_code):
                super().__init__(f"HTTP {status_code}")
                self.status_code = status_code

        class ClientError(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.response = {"Error": {"Code": code}}

        class NoCredentialsError(Exception):
            pass

        for exc in (ApiError(401), ApiError(403), ApiError(404),
                    ClientError("AccessDeniedException"),
                    ClientError("UnrecognizedClientException"), NoCredentialsError()):
            self.assertTrue(is_permanent(exc), repr(exc))
        for exc in (ApiError(429), ApiError(500), ApiError(529),
                    ClientError("ThrottlingException"), TimeoutError("slow"),
                    ConnectionError("reset")):
            self.assertFalse(is_permanent(exc), repr(exc))
        # and the worker stops on one instead of retrying
        agent = StubAgent(fail_times=99, exc=ApiError(401))
        worker, source, store, _ = make_worker(agent)
        source.put(GOOD)
        with self.assertRaises(ApiError):
            worker.run()
        self.assertEqual(len(agent.calls), 1)

    def test_batch_size_and_visibility_are_validated(self):
        with self.assertRaises(ValueError):
            Worker(StubAgent(), MemorySource(), AnswerStore(":memory:"), batch_size=0)
        with self.assertRaises(ValueError):
            MemorySource(visibility_timeout=0)

    def test_retry_with_backlog_keeps_arrival_order_for_the_rest(self):
        agent = StubAgent(fail_times=1)               # the first event fails once
        worker, source, store, _ = make_worker(agent)
        worker.batch_size = 2
        for i in range(4):
            source.put({"event_id": f"evt-{i}", "question": f"q{i}"})
        worker.run()
        self.assertEqual(agent.calls, ["q0", "q1", "q2", "q3", "q0"])
        self.assertEqual([r["event_id"] for r in store.answers()],
                         ["evt-1", "evt-2", "evt-3", "evt-0"])


class TestBackoff(unittest.TestCase):
    def test_exponential_capped_with_jitter_bounds(self):
        b = Backoff(base=0.5, cap=8.0, jitter=0.25, rand=lambda: 0.0)
        self.assertEqual([b.delay(a) for a in (1, 2, 3, 4, 5, 6)],
                         [0.5, 1.0, 2.0, 4.0, 8.0, 8.0])
        top = Backoff(base=0.5, cap=8.0, jitter=0.25, rand=lambda: 0.999)
        self.assertLess(top.delay(5), 8.0 * 1.25)
        self.assertGreater(top.delay(5), 8.0)


class TestStore(unittest.TestCase):
    def test_insert_is_idempotent_on_event_id(self):
        store = AnswerStore(":memory:")
        ev = parse_event(GOOD)
        self.assertTrue(store.insert_answer(ev, {"answer": "a", "citations": []},
                                            attempt=1, duration_ms=3))
        self.assertFalse(store.insert_answer(ev, {"answer": "b", "citations": []},
                                             attempt=2, duration_ms=3))
        self.assertEqual(store.answer("evt-1")["answer"], "a")
        self.assertTrue(store.has("evt-1"))
        self.assertFalse(store.has("nope"))

    def test_refusal_is_flagged(self):
        store = AnswerStore(":memory:")
        ev = parse_event(GOOD)
        store.insert_answer(ev, {"answer": "", "citations": []}, attempt=1,
                            duration_ms=1)
        self.assertEqual(store.answer("evt-1")["refused"], 1)


class TestFileSource(unittest.TestCase):
    def test_jsonl_with_a_poison_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.jsonl"
            p.write_text(json.dumps(GOOD) + "\n\nnot json\n"
                         + json.dumps({"event_id": "evt-2", "question": "q2"}) + "\n",
                         encoding="utf-8")
            clock = FakeClock()
            src = FileSource(p, clock=clock)
            self.assertEqual(src.pending(), 3)
            worker = Worker(StubAgent(), src, AnswerStore(":memory:"),
                            clock=clock, sleep=clock.advance)
            m = worker.run()
            self.assertEqual((m.processed, m.malformed), (2, 1))

    def test_utf8_bom_does_not_poison_the_first_event(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.jsonl"
            p.write_bytes(b"\xef\xbb\xbf" + json.dumps(GOOD).encode("utf-8") + b"\r\n")
            clock = FakeClock()
            src = FileSource(p, clock=clock)
            worker = Worker(StubAgent(), src, AnswerStore(":memory:"),
                            clock=clock, sleep=clock.advance)
            m = worker.run()
            self.assertEqual((m.processed, m.malformed), (1, 0))


# ---------------------------------------------------------------------------
# integration: the real ToolAgent over the local index, FakeLLM scripted
# ---------------------------------------------------------------------------


@unittest.skipUnless(Path("index/chunks.json").exists(),
                     "no index; run: python -m openedge_agent.ingest")
class TestConsumerWithToolAgent(unittest.TestCase):
    def test_answer_and_citations_persisted(self):
        from openedge_agent.agent3 import ToolAgent
        from openedge_agent.llm import FakeLLM

        reply = ("ar-invoice.p is called from the posting flow.\n"
                 "CITATIONS: corpus/oe/oe-post.p#main")
        agent = ToolAgent(index_dir="index", llm=FakeLLM([reply]), k=10)
        worker, source, store, _ = make_worker(agent)
        source.put(GOOD)
        worker.run()
        row = store.answer("evt-1")
        self.assertIn("posting flow", row["answer"])
        self.assertEqual(row["citations"], ["corpus/oe/oe-post.p#main"])
        self.assertEqual(row["refused"], 0)
        self.assertIsNotNone(row["steps"])

    def test_cli_over_a_file_with_the_fake_backend(self):
        from consumer.run import main

        with tempfile.TemporaryDirectory() as d:
            events = Path(d) / "events.jsonl"
            events.write_text(json.dumps(GOOD) + "\nnot json\n", encoding="utf-8")
            db = Path(d) / "answers.sqlite"
            saved = os.environ.get("OE_AGENT_BACKEND")
            os.environ["OE_AGENT_BACKEND"] = "fake"
            try:
                import io
                from contextlib import redirect_stdout
                from unittest import mock
                buf = io.StringIO()
                with redirect_stdout(buf), mock.patch("consumer.run.logging.basicConfig"):
                    code = main(["--input", str(events), "--db", str(db),
                                 "--backend", "fake", "--visibility", "1"])
            finally:
                if saved is None:
                    os.environ.pop("OE_AGENT_BACKEND", None)
                else:
                    os.environ["OE_AGENT_BACKEND"] = saved
            self.assertEqual(code, 1)                 # one dead letter -> exit 1
            metrics = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual((metrics["processed"], metrics["malformed"]), (1, 1))
            store = AnswerStore(db)
            self.assertEqual(store.counts(), {"answers": 1, "dead_letters": 1})
            store.close()

    def test_cli_stops_before_the_queue_when_the_backend_is_not_ready(self):
        from consumer.run import main
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            events = Path(d) / "events.jsonl"
            events.write_text(json.dumps(GOOD) + "\n", encoding="utf-8")
            db = Path(d) / "answers.sqlite"
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            import io
            from contextlib import redirect_stderr
            err = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("consumer.run.logging.basicConfig"), \
                    redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                main(["--input", str(events), "--db", str(db), "--backend", "anthropic"])
            self.assertEqual(cm.exception.code, 2)    # configuration, not a run failure
            self.assertIn("backend not ready", err.getvalue())
            self.assertFalse(db.exists())             # nothing fetched, nothing written


if __name__ == "__main__":
    unittest.main()
