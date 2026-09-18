"""Offline tests for the Bedrock backend and the backend chooser.

No AWS account, no boto3, no network, no cost: a stub client stands in for
bedrock-runtime and records exactly what was sent to it.
"""

import os
import unittest
from unittest import mock

from openedge_agent.llm import (
    AnthropicLLM,
    BedrockLLM,
    DEFAULT_MODEL,
    FakeLLM,
    make_llm,
    price_for,
)

MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


class StubBedrockClient:
    """Minimal stand-in for a boto3 bedrock-runtime client."""

    def __init__(self, text="hello from bedrock", usage=None):
        self.text = text
        self.usage = {"inputTokens": 120, "outputTokens": 34} if usage is None else usage
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"role": "assistant",
                                   "content": [{"text": self.text}]}},
            "usage": self.usage,
            "stopReason": "end_turn",
        }


class BedrockRequestShape(unittest.TestCase):
    def test_request_carries_system_user_and_inference_config(self):
        stub = StubBedrockClient()
        llm = BedrockLLM(model=MODEL_ID, client=stub)
        out = llm.complete("SYSTEM RULES", "the question", max_tokens=256)

        self.assertEqual(out, "hello from bedrock")
        self.assertEqual(len(stub.calls), 1)
        sent = stub.calls[0]
        self.assertEqual(sent["modelId"], MODEL_ID)
        self.assertEqual(sent["system"], [{"text": "SYSTEM RULES"}])
        self.assertEqual(
            sent["messages"],
            [{"role": "user", "content": [{"text": "the question"}]}],
        )
        self.assertEqual(sent["inferenceConfig"]["maxTokens"], 256)
        self.assertEqual(sent["inferenceConfig"]["temperature"], 0.0)

    def test_multiple_text_blocks_are_joined(self):
        stub = StubBedrockClient()
        stub.converse = lambda **kw: {
            "output": {"message": {"content": [{"text": "part one "},
                                               {"toolUse": {}},
                                               {"text": "part two"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 2},
        }
        llm = BedrockLLM(model=MODEL_ID, client=stub)
        self.assertEqual(llm.complete("s", "u"), "part one part two")


class BedrockMetering(unittest.TestCase):
    def test_real_token_counts_are_recorded_not_estimated(self):
        stub = StubBedrockClient()
        llm = BedrockLLM(model=MODEL_ID, client=stub)
        llm.complete("s", "u")
        llm.complete("s", "u")
        meta = llm.meter.as_dict()
        self.assertEqual(meta["calls"], 2)
        self.assertEqual(meta["input_tokens"], 240)
        self.assertEqual(meta["output_tokens"], 68)
        self.assertFalse(meta["tokens_estimated"])
        self.assertEqual(meta["model"], MODEL_ID)

    def test_missing_usage_falls_back_to_an_estimate_and_says_so(self):
        stub = StubBedrockClient(usage={})
        llm = BedrockLLM(model=MODEL_ID, client=stub)
        llm.complete("s", "u")
        self.assertTrue(llm.meter.as_dict()["tokens_estimated"])

    def test_bedrock_model_id_prices_as_sonnet(self):
        self.assertEqual(price_for(MODEL_ID), price_for("claude-sonnet-4-5"))


class BedrockGuards(unittest.TestCase):
    def test_no_model_id_raises_a_useful_error_before_any_call(self):
        with mock.patch.dict(os.environ, {"OE_BEDROCK_MODEL_ID": ""}, clear=False):
            llm = BedrockLLM()
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("s", "u")
        self.assertIn("OE_BEDROCK_MODEL_ID", str(ctx.exception))

    def test_region_defaults_and_can_be_overridden(self):
        with mock.patch.dict(os.environ, {"OE_BEDROCK_REGION": "", "AWS_REGION": ""},
                             clear=False):
            self.assertEqual(BedrockLLM(model=MODEL_ID).region, "us-east-1")
        self.assertEqual(BedrockLLM(model=MODEL_ID, region="us-west-2").region, "us-west-2")


class BackendChooser(unittest.TestCase):
    def test_default_is_unchanged_anthropic(self):
        with mock.patch.dict(os.environ, {"OE_AGENT_BACKEND": ""}, clear=False):
            self.assertIsInstance(make_llm(), AnthropicLLM)

    def test_env_var_selects_bedrock_and_fake(self):
        with mock.patch.dict(os.environ, {"OE_AGENT_BACKEND": "bedrock"}, clear=False):
            self.assertIsInstance(make_llm(), BedrockLLM)
        with mock.patch.dict(os.environ, {"OE_AGENT_BACKEND": "FAKE"}, clear=False):
            self.assertIsInstance(make_llm(), FakeLLM)

    def test_anthropic_default_model_is_not_passed_on_as_a_bedrock_id(self):
        with mock.patch.dict(os.environ,
                             {"OE_AGENT_BACKEND": "bedrock",
                              "OE_BEDROCK_MODEL_ID": MODEL_ID}, clear=False):
            llm = make_llm(model=DEFAULT_MODEL)
        self.assertEqual(llm.model, MODEL_ID)

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            make_llm(backend="azure")


if __name__ == "__main__":
    unittest.main()
