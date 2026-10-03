"""Offline tests for service/cli.py: run() is handed FastAPI's TestClient
(an httpx.Client), so the real code path runs in-process with no server."""

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

try:  # service deps are optional for the core suite (numpy only)
    import fastapi, httpx  # noqa: F401
except ImportError as exc:
    raise unittest.SkipTest(
        f"service tests need fastapi and httpx ({exc}); "
        "pip install -r service/requirements.txt"
    )

from fastapi.testclient import TestClient

from service.app import app
from service.cli import run

QUESTION = "what calls ar-invoice.p?"


class ServiceCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

    def test_health_prints_status(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = run(["health"], client=self.client)
        self.assertEqual(code, 0)
        self.assertIn('"status": "ok"', out.getvalue())
        self.assertIn('"backend": "fake"', out.getvalue())

    def test_ask_prints_the_answer_block(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = run(["ask", QUESTION], client=self.client)
        self.assertEqual(code, 0)
        text = out.getvalue()
        # FakeLLM's default reply is "CITATIONS: none" -> a refusal
        self.assertIn("(refused: not in the retrieved code)", text)
        self.assertIn("citations:", text)
        self.assertIn("steps    :", text)

    def test_non_200_returns_1_with_the_error_on_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run(["ask", "hi"], client=self.client)
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().startswith("error 422:"))
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
