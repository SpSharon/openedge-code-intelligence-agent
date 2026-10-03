"""Offline test for service/fanout.py: httpx.AsyncClient over ASGITransport
talks to the app in-process; TestClient is entered once so lifespan runs."""

import asyncio
import os
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from service.app import app
from service.fanout import load_cases, run_all

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "answers_heldout.jsonl"


class ServiceFanoutTests(unittest.TestCase):
    def test_fanout_returns_every_case_in_file_order(self):
        cases = load_cases(CASES)
        self.assertEqual(len(cases), 9)

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as client:
                return await run_all(client, cases, concurrency=3)

        saved = os.environ.get("OE_AGENT_BACKEND")
        os.environ["OE_AGENT_BACKEND"] = "fake"
        try:
            with TestClient(app):  # runs lifespan; ASGITransport does not
                results = asyncio.run(go())
        finally:
            if saved is None:
                os.environ.pop("OE_AGENT_BACKEND", None)
            else:
                os.environ["OE_AGENT_BACKEND"] = saved
        self.assertEqual([r["id"] for r in results], [c["id"] for c in cases])
        self.assertFalse([r for r in results if "error" in r])


if __name__ == "__main__":
    unittest.main()
