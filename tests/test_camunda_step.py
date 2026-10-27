"""Offline tests for camunda_step/ — the worker against a fake Camunda 7 REST
engine (standard library http.server in a thread; no Docker, no network beyond
localhost, no model), and the BPMN's shape. The three drills the bid names —
duplicate delivery, downstream failure, lock expiry — each have a test that
fails if the behaviour stops. Needs index/ (run ingest first, as the suite does)."""

import io
import json
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from camunda_step.engine import Engine, EngineError, EngineTransient, plain, typed
from camunda_step.run import BPMN, PROCESS_KEY
from camunda_step.worker import (MAX_STRING, PREVIEW_CHARS, PROMPT_VERSION_SERVED, DraftWorker, Store,
                                 is_transient, make_agent, one_line)

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "index"
NS = {"bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
      "camunda": "http://camunda.org/schema/1.0/bpmn",
      "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI"}
CAM = "{http://camunda.org/schema/1.0/bpmn}"


# ---------------------------------------------------------------------------
# a fake engine: the handful of endpoints the worker and the runner use
# ---------------------------------------------------------------------------


class FakeEngineState:
    def __init__(self):
        self.queue: list[dict] = []        # tasks waiting to be fetched
        self.completed: list[tuple] = []   # (task_id, worker_id, variables)
        self.failures: list[dict] = []     # the failure reports
        self.extensions: list[tuple] = []  # (task_id, worker_id, newDuration)
        self.deployments: list[dict] = []
        self.started: list[dict] = []
        self.locked: dict[str, str] = {}   # task_id -> workerId holding the lock
        self.reject_next_complete = None   # (status, body) to answer the next complete with
        self.reject_completes = 0          # answer this many completes with 500, then behave
        self.fail_fetch = None             # a status to answer fetchAndLock with
        self.fail_extend = None            # a status to answer extendLock with
        self.deploy_fields = []            # multipart field names seen by deployment/create
        self.lock = threading.Lock()


def make_handler(state: FakeEngineState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, status, payload=None):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type", "")
            return json.loads(raw) if raw and ctype.startswith("application/json") else raw

        def do_POST(self):
            p = self.path.split("?")[0]
            body = self._body()
            with state.lock:
                if p == "/engine-rest/external-task/fetchAndLock":
                    if state.fail_fetch:
                        return self._send(state.fail_fetch, {"message": "boom"})
                    n = body["maxTasks"]
                    out = []
                    for t in list(state.queue):
                        if t["topicName"] == body["topics"][0]["topicName"] and t["id"] not in state.locked:
                            state.locked[t["id"]] = body["workerId"]
                            out.append(t)
                            state.queue.remove(t)
                        if len(out) >= n:
                            break
                    return self._send(200, out)
                m = p.removeprefix("/engine-rest/external-task/")
                if m.endswith("/complete"):
                    tid = m[: -len("/complete")]
                    if state.reject_next_complete:
                        status, msg = state.reject_next_complete
                        state.reject_next_complete = None
                        return self._send(status, {"type": "RestException", "message": msg})
                    if state.reject_completes > 0:
                        state.reject_completes -= 1
                        state.locked.pop(tid, None)          # the lock lapses, as it would in time
                        return self._send(500, {"type": "ProcessEngineException", "message": "continuation failed"})
                    if state.locked.get(tid) != body["workerId"]:
                        return self._send(400, {"message": f"task {tid} is not locked by {body['workerId']}"})
                    state.completed.append((tid, body["workerId"], body["variables"]))
                    del state.locked[tid]
                    return self._send(204)
                if m.endswith("/failure"):
                    tid = m[: -len("/failure")]
                    state.failures.append({"id": tid, **body})
                    state.locked.pop(tid, None)
                    return self._send(204)
                if m.endswith("/extendLock"):
                    tid = m[: -len("/extendLock")]
                    if state.fail_extend:
                        return self._send(state.fail_extend, {"message": "lock is gone"})
                    state.extensions.append((tid, body["workerId"], body["newDuration"]))
                    return self._send(204)
                if p == "/engine-rest/deployment/create":
                    raw = body if isinstance(body, bytes) else b""
                    import re as _re
                    state.deploy_fields = _re.findall(rb'name="([^"]+)"', raw)
                    state.deployments.append({"bytes": len(raw), "has_bpmn": b"bpmn:process" in raw})
                    return self._send(200, {"id": "dep-1", "deployedProcessDefinitions":
                                            {"d1": {"key": PROCESS_KEY, "version": 1}}})
                if p.startswith("/engine-rest/process-definition/key/") and p.endswith("/start"):
                    key = p.split("/")[-2]
                    inst = {"id": f"inst-{len(state.started) + 1}", "businessKey": body.get("businessKey"),
                            "definitionKey": key, "variables": body["variables"]}
                    state.started.append(inst)
                    return self._send(200, {"id": inst["id"], "businessKey": inst["businessKey"]})
            return self._send(404, {"message": "unknown " + p})

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/engine-rest/task":
                return self._send(200, [])
            if p.startswith("/engine-rest/history/process-instance/"):
                return self._send(200, {"id": p.split("/")[-1], "processDefinitionKey": PROCESS_KEY,
                                        "state": "COMPLETED", "startTime": "t0", "endTime": "t1"})
            if p == "/engine-rest/history/activity-instance":
                return self._send(200, [{"activityType": "userTask", "activityName": "Review the draft",
                                         "startTime": "t0", "endTime": "t1", "assignee": "demo"}])
            if p == "/engine-rest/history/variable-instance":
                return self._send(200, [{"name": "decision", "value": "approve"},
                                        {"name": "prompt_version", "value": "v1"}])
            if p == "/engine-rest/history/user-operation-log":
                return self._send(200, [{"timestamp": "t1", "userId": "demo", "operationType": "Complete"}])
            return self._send(404, {"message": "unknown " + p})

    return Handler


class FakeEngine:
    """Runs the fake on a free localhost port for one test class."""

    def __init__(self):
        self.state = FakeEngineState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/engine-rest"

    def task(self, task_id="task-1", question="what calls ar-invoice.p?", **extra):
        variables = {"question": typed(question), "prompt_version": typed("v1"),
                     "approvalTimeout": typed("PT10M")}
        for k, v in extra.items():
            variables[k] = typed(v)
        t = {"id": task_id, "topicName": "draft-answer", "processInstanceId": "inst-1",
             "activityId": "draft", "retries": None, "variables": variables}
        self.state.queue.append(t)
        return t

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# agents for the drills
# ---------------------------------------------------------------------------


class CountingAgentFactory:
    """Builds fake-demo agents and counts how many were asked."""

    def __init__(self, backend="fake-demo", delay=0.0, raise_with=None, answer=None):
        self.calls = 0
        self.backend = backend
        self.delay = delay
        self.raise_with = raise_with
        self.answer = answer
        self.questions = []

    def __call__(self):
        factory = self

        class _Agent:
            def __init__(inner):
                inner.real = make_agent(factory.backend, INDEX)
                inner.llm = inner.real.llm

            def ask(inner, question):
                factory.calls += 1
                factory.questions.append(question)
                if factory.delay:
                    time.sleep(factory.delay)
                if factory.raise_with is not None:
                    raise factory.raise_with
                out = inner.real.ask(question)
                if factory.answer is not None:
                    out = dict(out, answer=factory.answer)
                return out
        return _Agent()


class RateLimitError(Exception):   # the name the anthropic SDK uses; status 429
    status_code = 429


class AuthenticationError(Exception):
    status_code = 401


def quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()) as buf:
        out = fn(*a, **kw)
    return out, [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class WorkerDrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (INDEX / "chunks.json").exists():
            raise unittest.SkipTest("index/ missing — run python -m openedge_agent.ingest")
        cls.fake = FakeEngine()
        cls.tmp = TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        cls.tmp.cleanup()

    def setUp(self):
        st = self.fake.state
        st.queue.clear(); st.completed.clear(); st.failures.clear(); st.extensions.clear()
        st.locked.clear(); st.reject_next_complete = None; st.fail_fetch = None
        st.reject_completes = 0; st.fail_extend = None; st.deploy_fields = []
        self.n = getattr(self, "n", 0) + 1
        self.store = Store(Path(self.tmp.name) / f"store-{id(self)}-{time.time_ns()}.sqlite")
        self.addCleanup(self.store.close)   # closed before the directory is removed (Windows), no sqlite3 warning
        self.engine = Engine(self.fake.url, timeout=5)

    def worker(self, factory, **kw):
        return DraftWorker(self.engine, factory, self.store, worker_id="w1", lock_ms=kw.pop("lock_ms", 60_000),
                           sleep=lambda s: None, **kw)

    # -- the happy path: the audit record ------------------------------------------

    def test_completes_with_the_audit_record(self):
        self.fake.task("t1")
        factory = CountingAgentFactory()
        outcomes, lines = quiet(self.worker(factory).run_once)
        self.assertEqual(outcomes, ["completed"])
        self.assertEqual(factory.calls, 1)
        (tid, wid, variables), = self.fake.state.completed
        self.assertEqual((tid, wid), ("t1", "w1"))
        v = plain(variables)
        for key in ("answer", "answer_preview", "answer_sha256", "citations", "refused", "model_id", "input_tokens",
                    "output_tokens", "prompt_version", "prompt_version_served", "draft_attempt",
                    "drafted_at", "drafted_by", "draft_ms", "agent_steps", "answer_truncated"):
            self.assertIn(key, v, key)
        self.assertEqual(v["prompt_version"], "v1")
        self.assertEqual(v["prompt_version_served"], PROMPT_VERSION_SERVED)
        self.assertEqual(v["draft_attempt"], 1)
        self.assertEqual(v["model_id"], "fake-llm")
        self.assertFalse(v["refused"])
        self.assertIn("corpus/", v["citations"])
        self.assertEqual(variables["input_tokens"]["type"], "Integer")
        self.assertEqual(variables["refused"]["type"], "Boolean")
        self.assertEqual(len(v["answer_sha256"]), 64)
        self.assertEqual([l["event"] for l in lines], ["completed"])
        self.assertNotIn("\n", v["answer_preview"])

    def test_the_preview_is_one_line_and_capped_but_the_answer_is_whole(self):
        multi = "First line of the draft.\n\nSecond paragraph, cited [corpus/x].\n" + "z" * 900
        factory = CountingAgentFactory(answer=multi)
        self.fake.task("t1")
        quiet(self.worker(factory).run_once)
        v = plain(self.fake.state.completed[0][2])
        self.assertEqual(v["answer"], multi, "the audit copy is untouched")
        self.assertNotIn("\n", v["answer_preview"])
        self.assertIn("First line of the draft. ⏎ Second paragraph", v["answer_preview"])
        self.assertLessEqual(len(v["answer_preview"]), PREVIEW_CHARS)
        self.assertTrue(v["answer_preview"].endswith("…"))
        self.assertEqual(one_line("a\nb"), "a ⏎ b")

    def test_plain_fake_backend_answers_nothing_and_is_not_counted_as_a_refusal(self):
        self.fake.task("t1")
        quiet(self.worker(CountingAgentFactory(backend="fake")).run_once)
        v = plain(self.fake.state.completed[0][2])
        self.assertEqual(v["answer"], "")
        self.assertEqual(v["citations"], "none")
        self.assertFalse(v["refused"], "a refusal is the agent's phrase, not an empty reply")

    def test_a_real_refusal_is_flagged(self):
        factory = CountingAgentFactory(answer="The answer is **not** in the retrieved code.")
        self.fake.task("t1")
        quiet(self.worker(factory).run_once)
        self.assertTrue(plain(self.fake.state.completed[0][2])["refused"])

    # -- drill 1: duplicate delivery --------------------------------------------------

    def test_duplicate_delivery_is_completed_from_the_store_without_a_second_model_call(self):
        factory = CountingAgentFactory()
        w = self.worker(factory)
        self.fake.task("t1")
        quiet(w.run_once)
        self.fake.task("t1")            # the engine hands the same task out again
        outcomes, lines = quiet(w.run_once)
        self.assertEqual(outcomes, ["duplicate"])
        self.assertEqual(factory.calls, 1, "the model must not be called twice for one task")
        first, second = [plain(c[2]) for c in self.fake.state.completed]
        self.assertEqual(first, second)
        self.assertEqual(w.metrics["duplicates"], 1)
        self.assertEqual(self.store.count(), 1)

    def test_a_crash_between_store_and_complete_is_a_duplicate_next_time(self):
        factory = CountingAgentFactory()
        w = self.worker(factory)
        self.fake.task("t1")
        self.fake.state.reject_next_complete = (500, "db down")      # the engine fails the completion
        outcomes, _ = quiet(w.run_once)
        self.assertEqual(outcomes, ["complete_transient"])
        self.assertEqual(self.store.count(), 1, "stored before the completion was attempted")
        self.fake.state.locked.clear()                                 # the lock expires ...
        self.fake.task("t1")                                           # ... and the engine hands the task out again
        outcomes, _ = quiet(w.run_once)
        self.assertEqual(outcomes, ["duplicate"])
        self.assertEqual(factory.calls, 1)

    def test_a_transient_engine_error_on_the_duplicate_path_does_not_end_the_loop(self):
        factory = CountingAgentFactory()
        w = self.worker(factory)
        self.fake.task("t1")
        quiet(w.run_once)                                              # stored and completed
        self.fake.state.locked.clear()
        self.fake.task("t1")                                           # delivered again ...
        self.fake.state.reject_next_complete = (503, "engine busy")    # ... and the engine is busy
        outcomes, lines = quiet(w.run_once)
        self.assertEqual(outcomes, ["complete_transient"])
        self.assertEqual(lines[-1]["event"], "duplicate_complete_transient")
        self.assertEqual(factory.calls, 1)
        self.fake.state.locked.clear()
        self.fake.task("t1")
        outcomes, _ = quiet(w.run_once)                                # and the next delivery is answered
        self.assertEqual(outcomes, ["duplicate"])

    def test_repeated_completion_failures_never_call_the_model_again_and_never_raise(self):
        factory = CountingAgentFactory()
        w = self.worker(factory)
        self.fake.state.reject_completes = 3                           # the continuation keeps failing
        for _ in range(4):
            self.fake.task("t1")
            outcomes, _ = quiet(w.run_once)
            self.assertIn(outcomes[0], ("complete_transient", "duplicate"))
        self.assertEqual(factory.calls, 1)
        self.assertEqual(len(self.fake.state.completed), 1)

    def test_a_handler_exception_is_logged_and_the_loop_goes_on(self):
        w = self.worker(CountingAgentFactory())
        w.handle = lambda task: (_ for _ in ()).throw(RuntimeError("bug in the handler"))
        self.fake.task("t1")
        outcomes, lines = quiet(w.run_once)
        self.assertEqual(outcomes, ["handler_error"])
        self.assertEqual(lines[-1]["event"], "handler_error")
        self.assertIn("RuntimeError", lines[-1]["error"])

    # -- drill 2: downstream failure ----------------------------------------------------

    def test_transient_failure_retries_with_growing_backoff_then_an_incident(self):
        factory = CountingAgentFactory(raise_with=RateLimitError("429"))
        w = self.worker(factory, backoff_ms=(5_000, 15_000), max_retries=3)
        reported = []
        for retries_from_engine in (None, 2, 1):
            self.fake.state.queue.clear()
            t = self.fake.task("t1")
            t["retries"] = retries_from_engine
            outcomes, lines = quiet(w.run_once)
            reported.append((outcomes[0], self.fake.state.failures[-1]["retries"],
                             self.fake.state.failures[-1]["retryTimeout"]))
        self.assertEqual(reported, [("retry", 2, 5_000), ("retry", 1, 15_000), ("incident", 0, 0)])
        self.assertEqual(w.metrics["retries"], 2)
        self.assertEqual(w.metrics["incidents"], 1)
        self.assertEqual(self.store.count(), 0, "nothing is stored for a failed call")

    def test_the_engines_retries_field_is_read_not_counted_locally(self):
        """A worker that counted on its own would report 2 then 1; the engine's
        field says 1 after a crash elsewhere, so the next report must be 0."""
        w = self.worker(CountingAgentFactory(raise_with=RateLimitError("429")), max_retries=3)
        for given, expected in ((None, 2), (1, 0)):
            self.fake.state.queue.clear()
            t = self.fake.task("t1")
            t["retries"] = given
            quiet(w.run_once)
            self.assertEqual(self.fake.state.failures[-1]["retries"], expected)

    def test_permanent_failure_is_an_incident_at_once(self):
        factory = CountingAgentFactory(raise_with=AuthenticationError("bad key"))
        w = self.worker(factory)
        self.fake.task("t1")
        outcomes, lines = quiet(w.run_once)
        self.assertEqual(outcomes, ["incident"])
        f = self.fake.state.failures[-1]
        self.assertEqual(f["retries"], 0)
        self.assertIn("AuthenticationError", f["errorMessage"])
        self.assertTrue(lines[-1]["permanent"])

    def test_transient_classification(self):
        self.assertTrue(is_transient(RateLimitError()))
        self.assertTrue(is_transient(EngineTransient("x")))
        self.assertTrue(is_transient(ConnectionError()))
        self.assertFalse(is_transient(AuthenticationError()))
        self.assertFalse(is_transient(ValueError("bug")))

        class InternalServerError(Exception):     # an SDK class with the status on it
            status_code = 502
        self.assertTrue(is_transient(InternalServerError()))

        class ClientError(Exception):             # botocore: the status is in the response metadata
            def __init__(self, code):
                self.response = {"ResponseMetadata": {"HTTPStatusCode": code}}
        self.assertTrue(is_transient(ClientError(503)))
        self.assertFalse(is_transient(ClientError(403)))

        class ThrottlingException(Exception):     # a botocore modeled exception, by name
            pass
        self.assertTrue(is_transient(ThrottlingException()))

    def test_an_unreachable_engine_is_logged_not_raised(self):
        self.fake.state.fail_fetch = 503
        outcomes, lines = quiet(self.worker(CountingAgentFactory()).run_once)
        self.assertEqual(outcomes, ["engine_unreachable"])
        self.assertEqual(lines[0]["event"], "engine_unreachable")

    # -- drill 3: lock expiry ----------------------------------------------------------

    def test_the_lock_is_extended_while_a_slow_call_runs(self):
        factory = CountingAgentFactory(delay=0.5)
        w = self.worker(factory, lock_ms=200)       # keep-alive every 100 ms
        self.fake.task("t1")
        outcomes, lines = quiet(w.run_once)
        self.assertEqual(outcomes, ["completed"])
        self.assertGreaterEqual(len(self.fake.state.extensions), 2)
        self.assertTrue(all(e[:2] == ("t1", "w1") and e[2] == 200 for e in self.fake.state.extensions))
        self.assertGreaterEqual(w.metrics["lock_extensions"], 2)

    def test_a_failed_lock_extension_stops_the_keep_alive_and_the_call_still_completes(self):
        factory = CountingAgentFactory(delay=0.4)
        w = self.worker(factory, lock_ms=200)
        self.fake.state.fail_extend = 400                               # "the lock is gone"
        self.fake.task("t1")
        outcomes, lines = quiet(w.run_once)
        events = [l["event"] for l in lines]
        self.assertIn("lock_extend_failed", events)
        self.assertEqual(events.count("lock_extend_failed"), 1, "the keep-alive stops after one failure")
        self.assertEqual(outcomes, ["completed"])

    def test_a_lost_lock_is_reported_not_raised(self):
        self.fake.task("t1")
        self.fake.state.reject_next_complete = (400, "not locked by w1")
        outcomes, lines = quiet(self.worker(CountingAgentFactory()).run_once)
        self.assertEqual(outcomes, ["lost_lock"])
        self.assertEqual(lines[-1]["event"], "complete_rejected")

    # -- the review loop and the engine's limits ------------------------------------------

    def test_a_rejection_note_travels_back_to_the_model(self):
        factory = CountingAgentFactory()
        self.fake.task("t2", draft_attempt=1, decision="reject", reviewer_note="cite the caller too")
        quiet(self.worker(factory).run_once)
        self.assertIn("The reviewer rejected the previous draft with this note: cite the caller too",
                      factory.questions[0])
        self.assertEqual(plain(self.fake.state.completed[0][2])["draft_attempt"], 2)

    def test_a_long_answer_is_truncated_flagged_and_hashed(self):
        long_answer = "x" * (MAX_STRING + 500)
        factory = CountingAgentFactory(answer=long_answer)
        self.fake.task("t1")
        quiet(self.worker(factory).run_once)
        v = plain(self.fake.state.completed[0][2])
        self.assertEqual(len(v["answer"]), MAX_STRING)
        self.assertTrue(v["answer_truncated"])
        import hashlib
        self.assertEqual(v["answer_sha256"], hashlib.sha256(long_answer.encode()).hexdigest())

    def test_a_task_without_a_question_is_an_incident(self):
        self.fake.task("t1", question="")
        outcomes, _ = quiet(self.worker(CountingAgentFactory()).run_once)
        self.assertEqual(outcomes, ["incident"])
        self.assertEqual(self.fake.state.failures[-1]["retries"], 0)


class EngineClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeEngine()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()

    def test_typed_variables(self):
        self.assertEqual(typed(True), {"value": True, "type": "Boolean"})
        self.assertEqual(typed(7), {"value": 7, "type": "Integer"})
        self.assertEqual(typed(2**40), {"value": 2**40, "type": "Long"})
        self.assertEqual(typed("s"), {"value": "s", "type": "String"})
        self.assertEqual(typed(None), {"value": None, "type": "Null"})
        self.assertEqual(plain({"a": {"value": 1, "type": "Integer"}}), {"a": 1})

    def test_deploy_sends_the_bpmn_as_multipart_with_the_engines_field_names(self):
        dep = Engine(self.fake.url).deploy("governed-answer", BPMN)
        self.assertEqual(dep["id"], "dep-1")
        self.assertTrue(self.fake.state.deployments[-1]["has_bpmn"])
        fields = [f.decode() for f in self.fake.state.deploy_fields]
        for expected in ("deployment-name", "enable-duplicate-filtering", "deploy-changed-only",
                         "deployment-source", "governed-answer.bpmn"):
            self.assertIn(expected, fields)

    def test_start_sends_typed_variables_and_a_business_key(self):
        inst = Engine(self.fake.url).start(PROCESS_KEY, {"question": "q", "approvalTimeout": "PT2M"},
                                           business_key="demo-1")
        self.assertEqual(inst["businessKey"], "demo-1")
        sent = self.fake.state.started[-1]["variables"]
        self.assertEqual(sent["question"], {"value": "q", "type": "String"})

    def test_errors_are_classified(self):
        e = Engine(self.fake.url)
        with self.assertRaises(EngineError):
            e.complete("no-such-task", "w1", {})
        dead = Engine("http://127.0.0.1:9/engine-rest", timeout=0.5)
        with self.assertRaises(EngineTransient):
            dead.fetch_and_lock("w1", "draft-answer", 1000)

    def test_runner_history_prints_the_audit_trail(self):
        from camunda_step import run as runner
        with redirect_stdout(io.StringIO()) as buf:
            rc = runner.main(["--engine", self.fake.url, "history", "inst-1"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        for expected in ("state=COMPLETED", "Review the draft", "by demo", "demo  Complete",
                         "decision", '"approve"', "prompt_version"):
            self.assertIn(expected, text)

    def test_runner_rejects_a_timeout_that_is_not_a_duration(self):
        from camunda_step import run as runner
        before = len(self.fake.state.started)
        with redirect_stdout(io.StringIO()):
            rc = runner.main(["--engine", self.fake.url, "start", "--timeout", "10 minutes"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.fake.state.started), before, "nothing started")

    def test_runner_deploy_and_start(self):
        from camunda_step import run as runner
        rc, _ = quiet(runner.main, ["--engine", self.fake.url, "deploy"])
        self.assertEqual(rc, 0)
        rc, _ = quiet(runner.main, ["--engine", self.fake.url, "start", "--instances", "2", "--timeout", "PT2M"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.fake.state.started), 2)
        self.assertEqual(plain(self.fake.state.started[0]["variables"])["approvalTimeout"], "PT2M")


class ProcessShapeTests(unittest.TestCase):
    """The BPMN carries the shape the bid describes — checked by reading the XML,
    which is what the engine does; running it is the owner's live check."""

    @classmethod
    def setUpClass(cls):
        cls.root = ET.parse(BPMN).getroot()
        cls.process = cls.root.find("bpmn:process", NS)
        cls.by_id = {el.get("id"): el for el in cls.process}

    def test_executable_process_with_the_expected_key(self):
        self.assertEqual(self.process.get("id"), PROCESS_KEY)
        self.assertEqual(self.process.get("isExecutable"), "true")

    def test_the_draft_is_an_external_task_with_a_save_point_before_it(self):
        draft = self.by_id["draft"]
        self.assertEqual(draft.tag, f"{{{NS['bpmn']}}}serviceTask")
        self.assertEqual(draft.get(CAM + "type"), "external")
        self.assertEqual(draft.get(CAM + "topic"), "draft-answer")
        self.assertEqual(draft.get(CAM + "asyncBefore"), "true")
        self.assertEqual(draft.get(CAM + "asyncAfter"), "true",
                         "the completion must commit before the continuation (the timer) runs")

    def test_the_review_is_a_user_task_showing_the_record_and_asking_for_a_decision(self):
        review = self.by_id["review"]
        self.assertEqual(review.tag, f"{{{NS['bpmn']}}}userTask")
        fields = {f.get("id"): f for f in review.iter(CAM + "formField")}
        for shown in ("answer_preview", "citations", "model_id", "prompt_version_served"):
            self.assertIn(shown, fields)
            self.assertIsNotNone(fields[shown].find(f"{CAM}validation/{CAM}constraint[@name='readonly']"))
        self.assertNotIn("answer", fields, "the multi-line answer never goes through a single-line input")
        self.assertEqual(fields["decision"].get("type"), "enum")
        self.assertEqual({v.get("id") for v in fields["decision"].iter(CAM + "value")}, {"approve", "reject"})
        self.assertIn("reviewer_note", fields)

    def test_the_gateway_publishes_on_approve_and_loops_back_on_reject(self):
        gw = self.by_id["decision_gateway"]
        self.assertEqual(gw.get("default"), "flow_reject_draft")
        approve = self.by_id["flow_approve_published"]
        self.assertEqual(approve.get("targetRef"), "published")
        self.assertIn("execution.getVariable('decision') == 'approve'",
                      approve.find("bpmn:conditionExpression", NS).text,
                      "a missing decision must fall to the default flow, not throw")
        reject = self.by_id["flow_reject_draft"]
        self.assertEqual((reject.get("sourceRef"), reject.get("targetRef")), ("decision_gateway", "draft"))

    def test_an_unreviewed_draft_escalates_and_is_never_published(self):
        timer = self.by_id["review_timeout"]
        self.assertEqual(timer.get("attachedToRef"), "review")
        self.assertEqual(timer.get("cancelActivity"), "true")
        duration = timer.find("bpmn:timerEventDefinition/bpmn:timeDuration", NS)
        self.assertEqual(duration.text, "${approvalTimeout}")
        esc = self.by_id["flow_timeout_escalated"]
        self.assertEqual(esc.get("targetRef"), "escalated")
        self.assertEqual(self.by_id["escalated"].tag, f"{{{NS['bpmn']}}}endEvent")

    def test_every_flow_connects_known_elements_and_the_diagram_covers_them(self):
        ids = set(self.by_id)
        for flow in self.process.findall("bpmn:sequenceFlow", NS):
            self.assertIn(flow.get("sourceRef"), ids)
            self.assertIn(flow.get("targetRef"), ids)
        drawn = {s.get("bpmnElement") for s in self.root.iter(f"{{{NS['bpmndi']}}}BPMNShape")} | \
                {e.get("bpmnElement") for e in self.root.iter(f"{{{NS['bpmndi']}}}BPMNEdge")}
        self.assertEqual(drawn, ids, "every element and flow has a shape or edge, so Cockpit can draw it")

    def test_the_start_form_asks_for_the_three_inputs(self):
        start = self.by_id["start"]
        fields = {f.get("id") for f in start.iter(CAM + "formField")}
        self.assertEqual(fields, {"question", "prompt_version", "approvalTimeout"})


if __name__ == "__main__":
    unittest.main()
