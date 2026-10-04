"""Offline tests for service/app.py (fake backend; no network, no key)."""

import asyncio
import json
import os
import sys
import types
import unittest
from unittest import mock

try:  # service deps are optional for the core suite (numpy only)
    import fastapi, httpx  # noqa: F401
except ImportError as exc:
    raise unittest.SkipTest(
        f"service tests need fastapi and httpx ({exc}); "
        "pip install -r service/requirements.txt"
    )

import httpx
from fastapi.testclient import TestClient

import service.app as service_app
from service.app import app, settings

QUESTION = "what calls ar-invoice.p?"
ASK_KEYS = {"answer", "citations", "invalid_citations", "retrieved",
            "steps", "backend", "usage"}


class ServiceAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Configure, then start: lifespan reads the backend at start-up.
        cls._saved_backend = os.environ.get("OE_AGENT_BACKEND")
        os.environ["OE_AGENT_BACKEND"] = "fake"
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        if cls._saved_backend is None:
            os.environ.pop("OE_AGENT_BACKEND", None)
        else:
            os.environ["OE_AGENT_BACKEND"] = cls._saved_backend

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["backend"], "fake")
        self.assertGreater(body["units"], 30)
        self.assertIsNone(body["problem"])
        # An unknown backend is reported by /health (200, "misconfigured")
        # from a fresh lifespan start; the shared settings are restored after.
        saved_settings = dict(settings)
        saved_env = os.environ.get("OE_AGENT_BACKEND")
        os.environ["OE_AGENT_BACKEND"] = "nonsense"
        try:
            with TestClient(app) as fresh:
                r = fresh.get("/health")
        finally:
            os.environ["OE_AGENT_BACKEND"] = saved_env
            settings.clear()
            settings.update(saved_settings)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "misconfigured")
        self.assertIn("unknown backend 'nonsense'", r.json()["problem"])

    def test_ask_returns_the_agent_shape(self):
        r = self.client.post("/ask", json={"question": QUESTION})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body), ASK_KEYS)
        self.assertEqual(len(body["retrieved"]), 10)  # k's default
        self.assertEqual(body["backend"], "fake")
        self.assertEqual(body["usage"]["model"], "fake-llm")

    def test_validation_rejects_a_bad_body(self):
        with mock.patch.object(service_app, "ToolAgent") as agent_cls:
            for bad in ({"question": "hi"},
                        {"question": "   "},          # stripped, then too short
                        {"question": QUESTION, "k": 0},
                        {"question": QUESTION, "max_steps": 0}):
                with self.subTest(body=bad):
                    r = self.client.post("/ask", json=bad)
                    self.assertEqual(r.status_code, 422)
                    self.assertEqual(len(r.json()["detail"]), 1)  # reported once
            agent_cls.assert_not_called()  # never reached the agent

    def test_configuration_errors_are_503_and_bugs_are_500(self):
        saved_backend = settings["backend"]
        saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.subTest("unknown backend"):
                settings["backend"] = "nonsense"
                r = self.client.post("/ask", json={"question": QUESTION})
                self.assertEqual(r.status_code, 503)
                self.assertIn("unknown backend", r.json()["detail"])
            with self.subTest("unknown backend, stream refuses before starting"):
                r = self.client.post("/ask/stream", json={"question": QUESTION})
                self.assertEqual(r.status_code, 503)
                self.assertIn("unknown backend", r.json()["detail"])
            with self.subTest("missing API key"):
                settings["backend"] = "anthropic"
                r = self.client.post("/ask", json={"question": QUESTION})
                self.assertEqual(r.status_code, 503)
                self.assertIn("ANTHROPIC_API_KEY", r.json()["detail"])
            with self.subTest("bedrock with a model id but no AWS credentials"):
                # boto3 is not installed in the offline build environment, so
                # a stand-in module is injected: its credential chain resolves
                # to None, as real boto3's Session().get_credentials() does
                # when no credentials exist. This tests the service's branch,
                # not boto3 itself.
                fake_boto3 = types.ModuleType("boto3")
                fake_boto3.client = mock.Mock(return_value=object())
                fake_boto3.Session = mock.Mock(return_value=mock.Mock(
                    get_credentials=mock.Mock(return_value=None)))
                settings["backend"] = "bedrock"
                with mock.patch.dict(sys.modules, {"boto3": fake_boto3}), \
                        mock.patch.dict(os.environ,
                                        {"OE_BEDROCK_MODEL_ID": "test-model-id"}):
                    r = self.client.post("/ask", json={"question": QUESTION})
                self.assertEqual(r.status_code, 503)
                self.assertIn("no AWS credentials", r.json()["detail"])
        finally:
            settings["backend"] = saved_backend
            if saved_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved_key
        # No `with`: lifespan already ran for the class client; close() below.
        raw = TestClient(app, raise_server_exceptions=False)
        try:
            with self.subTest("a bug inside the agent stays a 500"):
                # Even a RuntimeError raised by ask() is not relabelled as 503.
                with mock.patch.object(service_app.ToolAgent, "ask",
                                       side_effect=RuntimeError("simulated bug")):
                    r = raw.post("/ask", json={"question": QUESTION})
                self.assertEqual(r.status_code, 500)
            with self.subTest("a failure mid-stream truncates the stream"):
                # Documented limit: the 200 is already sent, so the client
                # gets the "retrieved" line and no "answer" line. httpx's
                # ASGITransport keeps the partial body (TestClient drops it).
                async def partial_stream():
                    transport = httpx.ASGITransport(app=app,
                                                   raise_app_exceptions=False)
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as c:
                        return await c.post("/ask/stream",
                                            json={"question": QUESTION})

                with mock.patch.object(service_app.ToolAgent, "ask",
                                       side_effect=RuntimeError("simulated bug")):
                    r = asyncio.run(partial_stream())
                self.assertEqual(r.status_code, 200)
                events = [json.loads(line) for line in r.text.splitlines() if line]
                self.assertEqual([e["event"] for e in events], ["retrieved"])
        finally:
            raw.close()

    def test_stream_is_json_lines_with_two_events(self):
        with self.client.stream("POST", "/ask/stream",
                                json={"question": QUESTION}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("application/jsonl"))
            events = [json.loads(line) for line in r.iter_lines() if line]
        self.assertEqual([e["event"] for e in events], ["retrieved", "answer"])
        self.assertEqual(len(events[0]["data"]["ids"]), 10)
        self.assertIn("citations", events[1]["data"])


if __name__ == "__main__":
    unittest.main()
