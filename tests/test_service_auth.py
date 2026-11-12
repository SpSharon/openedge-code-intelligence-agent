"""Offline tests for the service access key (fake backend; no network, no
AWS, no real key). Brief 2026-10-04, requirements A2, A3, A5 and A6."""

import asyncio
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

import service.app as service_app
from service.app import app
from service.cli import run as cli_run
import service.fanout as fanout
from service.fanout import load_cases, run_all

QUESTION = "what calls ar-invoice.p?"
TEST_KEY = "test-key-0123456789abcdef"  # offline tests only; not a real key
CASES = Path(__file__).resolve().parent.parent / "evals" / "answers_heldout.jsonl"


def _env(**extra):
    """The fake backend, and no key from the developer's own shell."""
    env = {"OE_AGENT_BACKEND": "fake"}
    env.update(extra)
    patch = mock.patch.dict(os.environ, env)
    patch.start()
    for name in ("OE_SERVICE_KEY", "OE_SERVICE_KEY_SSM_PARAM"):
        if name not in extra:
            os.environ.pop(name, None)
    return patch


def _fake_aws(value=None, error=None, response=None):
    """Stand-ins for boto3 and botocore.exceptions (not installed in the
    offline build environment). get_parameter returns `value` or raises
    `error`; this tests the service's branches, not boto3 itself."""
    exceptions = types.ModuleType("botocore.exceptions")

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        pass

    exceptions.BotoCoreError = BotoCoreError
    exceptions.ClientError = ClientError
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions

    ssm = mock.Mock()
    if error is not None:
        ssm.get_parameter.side_effect = getattr(exceptions, error)("denied")
    elif response is not None:
        ssm.get_parameter.return_value = response
    else:
        ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
    boto3 = types.ModuleType("boto3")
    boto3.client = mock.Mock(return_value=ssm)
    return {"boto3": boto3, "botocore": botocore,
            "botocore.exceptions": exceptions}, ssm


class AccessKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = _env(OE_SERVICE_KEY=TEST_KEY)
        cls.client = TestClient(app)  # no default header: each test chooses
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls._env.stop()

    def test_missing_or_wrong_key_is_401_before_the_agent(self):
        good = {"question": QUESTION}
        with mock.patch.object(service_app, "ToolAgent") as agent_cls:
            for path in ("/ask", "/ask/stream"):
                for headers in ({}, {"X-API-Key": "wrong-key-0123456789"},
                                {"X-API-Key": TEST_KEY + "x"},
                                {"X-API-Key": ""}):
                    with self.subTest(path=path, headers=headers):
                        r = self.client.post(path, json=good, headers=headers)
                        self.assertEqual(r.status_code, 401)
                        self.assertNotIn(TEST_KEY, r.text)
            with self.subTest("a bad body without a key is 401, not 422"):
                r = self.client.post("/ask", json={"question": "hi"})
                self.assertEqual(r.status_code, 401)
            agent_cls.assert_not_called()  # never built, never called

    def test_the_right_key_is_accepted(self):
        h = {"X-API-Key": TEST_KEY}
        self.assertEqual(self.client.post(
            "/ask", json={"question": QUESTION}, headers=h).status_code, 200)
        with self.client.stream("POST", "/ask/stream",
                                json={"question": QUESTION}, headers=h) as r:
            self.assertEqual(r.status_code, 200)
        # A bad body with the right key is still the usual 422.
        self.assertEqual(self.client.post(
            "/ask", json={"question": "hi"}, headers=h).status_code, 422)

    def test_health_is_open_and_reveals_no_key(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["key_configured"])
        self.assertNotIn(TEST_KEY, r.text)

    def test_docs_offer_the_authorize_button(self):
        spec = self.client.get("/openapi.json").json()
        schemes = spec["components"]["securitySchemes"]
        self.assertEqual(list(schemes.values()),
                         [{"type": "apiKey", "in": "header",
                           "name": "X-API-Key",
                           "description": "The service access key"}])
        self.assertIn("security", spec["paths"]["/ask"]["post"])
        self.assertIn("security", spec["paths"]["/ask/stream"]["post"])
        self.assertNotIn("security", spec["paths"]["/health"]["get"])
        for path in ("/ask", "/ask/stream"):
            self.assertLessEqual({"401", "503"},
                                 set(spec["paths"][path]["post"]["responses"]))
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_cli_sends_the_key_from_flag_or_environment(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with mock.patch.dict(os.environ):
                os.environ.pop("OE_SERVICE_KEY")
                no_key = cli_run(["ask", QUESTION], client=self.client)
                with_flag = cli_run(["--key", TEST_KEY, "ask", QUESTION],
                                    client=self.client)
            from_env = cli_run(["ask", QUESTION], client=self.client)
        self.assertEqual((no_key, with_flag, from_env), (1, 0, 0))
        self.assertTrue(err.getvalue().startswith("error 401:"))

    def test_fanout_sends_the_key(self):
        cases = load_cases(CASES)

        async def go(key):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as client:
                return await run_all(client, cases, concurrency=3, key=key)

        with_key = asyncio.run(go(TEST_KEY))
        without = asyncio.run(go(None))
        self.assertFalse([r for r in with_key if "error" in r])
        self.assertEqual(len(without), 9)
        self.assertTrue(all("401" in r["error"] for r in without))

    def test_fanout_main_takes_the_key_from_flag_or_environment(self):
        cases = str(CASES)
        with mock.patch.object(fanout, "main_async",
                               mock.AsyncMock(return_value=0)) as main_async:
            fanout.main(["--cases", cases, "--key", "flag-key-0123456789ab"])
            fanout.main(["--cases", cases])               # OE_SERVICE_KEY
            with mock.patch.dict(os.environ):
                os.environ.pop("OE_SERVICE_KEY")
                fanout.main(["--cases", cases])           # no key at all
        keys = [c.args[3] for c in main_async.call_args_list]
        self.assertEqual(keys, ["flag-key-0123456789ab", TEST_KEY, None])


class KeyConfigurationTests(unittest.TestCase):
    """Each test starts the app (lifespan) with its own environment."""

    def _start(self, **env):
        patch = _env(**env)
        self.addCleanup(patch.stop)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def assert_refused(self, client, reason):
        for path in ("/ask", "/ask/stream"):
            r = client.post(path, json={"question": QUESTION},
                            headers={"X-API-Key": TEST_KEY})
            self.assertEqual(r.status_code, 503, path)
            self.assertIn(reason, r.json()["detail"])
        health = client.get("/health").json()
        self.assertFalse(health["key_configured"])

    def test_no_key_configured_is_503_never_open(self):
        with mock.patch.object(service_app, "ToolAgent") as agent_cls:
            self.assert_refused(self._start(), "service key not configured")
            agent_cls.assert_not_called()

    def test_a_short_key_is_refused(self):
        self.assert_refused(self._start(OE_SERVICE_KEY="short"),
                            "shorter than 16 characters")

    def test_key_from_ssm_parameter(self):
        modules, ssm = _fake_aws(value=TEST_KEY + "\n")
        with mock.patch.dict(sys.modules, modules):
            client = self._start(OE_SERVICE_KEY_SSM_PARAM="/oe-agent/key")
        ssm.get_parameter.assert_called_once_with(Name="/oe-agent/key",
                                                  WithDecryption=True)
        r = client.post("/ask", json={"question": QUESTION},
                        headers={"X-API-Key": TEST_KEY})
        self.assertEqual(r.status_code, 200)

    def test_ssm_failure_is_503_with_no_detail_leak(self):
        for error in ("ClientError", "BotoCoreError"):
            with self.subTest(error=error):
                modules, _ = _fake_aws(error=error)
                with mock.patch.dict(sys.modules, modules):
                    client = self._start(
                        OE_SERVICE_KEY_SSM_PARAM="/oe-agent/key")
                self.assert_refused(client, f"({error})")

    def test_a_failed_ssm_read_is_retried_after_the_cooldown(self):
        modules, ssm = _fake_aws(error="ClientError")
        with mock.patch.dict(sys.modules, modules):
            client = self._start(OE_SERVICE_KEY_SSM_PARAM="/oe-agent/key")
            h = {"X-API-Key": TEST_KEY}
            ask = lambda: client.post("/ask", json={"question": QUESTION},
                                      headers=h).status_code
            ssm.get_parameter.side_effect = None
            ssm.get_parameter.return_value = {"Parameter": {"Value": TEST_KEY}}
            self.assertEqual(ask(), 503)          # inside the cooldown
            self.assertEqual(ssm.get_parameter.call_count, 1)
            service_app.settings["key_loaded_at"] -= service_app.SSM_RETRY_SECONDS
            self.assertEqual(ask(), 200)          # retried, now loaded
            self.assertEqual(ask(), 200)
            self.assertEqual(ssm.get_parameter.call_count, 2)

    def test_an_unexpected_ssm_error_is_not_swallowed(self):
        # Only BotoCoreError/ClientError mean "configuration"; anything else
        # (here a malformed response) is a bug and must surface.
        modules, _ = _fake_aws(response={})
        patch = _env(OE_SERVICE_KEY_SSM_PARAM="/oe-agent/key")
        self.addCleanup(patch.stop)
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaises(KeyError):
                with TestClient(app):
                    pass


if __name__ == "__main__":
    unittest.main()
