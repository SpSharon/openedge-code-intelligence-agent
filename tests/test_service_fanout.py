"""Offline test for service/fanout.py: httpx.AsyncClient over ASGITransport
talks to the app in-process; TestClient is entered once so lifespan runs."""

import asyncio
import os
import unittest
from pathlib import Path
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

from service.app import app
from service.fanout import load_cases, run_all

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "answers_heldout.jsonl"
TEST_KEY = "test-key-0123456789abcdef"  # offline tests only; not a real key


class ServiceFanoutTests(unittest.TestCase):
    def test_fanout_returns_every_case_in_file_order(self):
        cases = load_cases(CASES)
        self.assertEqual(len(cases), 9)

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as client:
                return await run_all(client, cases, concurrency=3,
                                     key=TEST_KEY)

        with mock.patch.dict(os.environ, {"OE_AGENT_BACKEND": "fake",
                                          "OE_SERVICE_KEY": TEST_KEY}):
            with TestClient(app):  # runs lifespan; ASGITransport does not
                results = asyncio.run(go())
        self.assertEqual([r["id"] for r in results], [c["id"] for c in cases])
        self.assertFalse([r for r in results if "error" in r])


if __name__ == "__main__":
    unittest.main()
