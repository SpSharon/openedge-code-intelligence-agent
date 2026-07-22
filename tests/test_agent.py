"""Offline tests for the Stage 2 agent: grounding plumbing, citation
parsing, refusal handling, hallucinated-citation filtering. All FakeLLM;
no network, no key."""

import unittest

from openedge_agent.agent import Agent, parse_response
from openedge_agent.llm import FakeLLM, Meter, approx_tokens

RETRIEVED = [
    "corpus/oe/oe-post.p#main",
    "corpus/ar/ar-invoice.p#main",
    "schema:ArHist",
]


class TestParseResponse(unittest.TestCase):
    def test_citations_line_semicolons(self):
        out = parse_response(
            "Posting invoices shipped orders.\n"
            "CITATIONS: corpus/oe/oe-post.p#main; corpus/ar/ar-invoice.p#main",
            RETRIEVED,
        )
        self.assertEqual(
            out["citations"],
            ["corpus/oe/oe-post.p#main", "corpus/ar/ar-invoice.p#main"],
        )
        self.assertEqual(out["answer"], "Posting invoices shipped orders.")
        self.assertEqual(out["invalid_citations"], [])

    def test_citations_line_commas_brackets_trailing_period(self):
        out = parse_response(
            "Answer.\nCitations: [corpus/oe/oe-post.p#main], [schema:ArHist].",
            RETRIEVED,
        )
        self.assertEqual(
            out["citations"], ["corpus/oe/oe-post.p#main", "schema:ArHist"]
        )

    def test_none_citations(self):
        out = parse_response(
            "That is not in the retrieved code.\nCITATIONS: none", RETRIEVED
        )
        self.assertEqual(out["citations"], [])
        self.assertEqual(out["invalid_citations"], [])

    def test_unretrieved_citation_flagged_not_kept(self):
        out = parse_response(
            "Made-up claim.\nCITATIONS: corpus/oe/oe-post.p#main; "
            "corpus/gl/gl-post.p#main",
            RETRIEVED,
        )
        self.assertEqual(out["citations"], ["corpus/oe/oe-post.p#main"])
        self.assertEqual(out["invalid_citations"], ["corpus/gl/gl-post.p#main"])

    def test_missing_corpus_prefix_restored(self):
        out = parse_response(
            "Answer.\nCITATIONS: oe/oe-post.p#main", RETRIEVED
        )
        self.assertEqual(out["citations"], ["corpus/oe/oe-post.p#main"])

    def test_case_insensitive_match(self):
        out = parse_response(
            "Answer.\nCITATIONS: schema:arhist", RETRIEVED
        )
        self.assertEqual(out["citations"], ["schema:ArHist"])

    def test_fallback_inline_ids_when_no_citations_line(self):
        out = parse_response(
            "Posting runs [corpus/ar/ar-invoice.p#main] to invoice.", RETRIEVED
        )
        self.assertEqual(out["citations"], ["corpus/ar/ar-invoice.p#main"])
        self.assertIn("Posting runs", out["answer"])

    def test_duplicates_deduped_order_kept(self):
        out = parse_response(
            "A.\nCITATIONS: schema:ArHist; corpus/oe/oe-post.p#main; schema:ArHist",
            RETRIEVED,
        )
        self.assertEqual(
            out["citations"], ["schema:ArHist", "corpus/oe/oe-post.p#main"]
        )

    def test_last_citations_line_wins(self):
        out = parse_response(
            "CITATIONS: schema:ArHist\nmore text\nCITATIONS: corpus/oe/oe-post.p#main",
            RETRIEVED,
        )
        self.assertEqual(out["citations"], ["corpus/oe/oe-post.p#main"])


class TestAgentOffline(unittest.TestCase):
    """End-to-end against the real index with a scripted LLM."""

    def _agent(self, responses):
        return Agent(index_dir="index", llm=FakeLLM(responses), k=10)

    def test_prompt_contains_question_and_retrieved_units(self):
        fake = FakeLLM(["x\nCITATIONS: none"])
        agent = Agent(index_dir="index", llm=fake, k=10)
        out = agent.ask("What happens when an order is posted?")
        system, user = fake.prompts[0]
        self.assertIn("What happens when an order is posted?", user)
        # every retrieved unit id appears labelled in the context block
        for uid in out["retrieved"]:
            self.assertIn(f"[{uid}]", user)
        # and the units' actual code text is present (spot-check one)
        self.assertIn("GROUNDING", system)
        self.assertEqual(len(out["retrieved"]), 10)

    def test_citations_parsed_from_reply_end_to_end(self):
        def cite_first_two(system, user):
            import re
            ids = re.findall(
                r"^\[((?:corpus/|schema:)[^\]]+)\]", user, flags=re.MULTILINE
            )
            return f"Grounded answer.\nCITATIONS: {ids[0]}; {ids[1]}"

        agent = self._agent(cite_first_two)
        out = agent.ask("what calls ar-invoice.p?")
        self.assertEqual(len(out["citations"]), 2)
        for c in out["citations"]:
            self.assertIn(c, out["retrieved"])

    def test_refusal_reply_yields_no_citations(self):
        agent = self._agent(
            ["The GL package is not in the retrieved code.\nCITATIONS: none"]
        )
        out = agent.ask("How does the GL cash-receipts posting work?")
        self.assertEqual(out["citations"], [])
        self.assertIn("not in the retrieved code", out["answer"])

    def test_hallucinated_citation_filtered(self):
        agent = self._agent(
            ["Claim.\nCITATIONS: corpus/gl/imaginary.p#main"]
        )
        out = agent.ask("What happens when an order is posted?")
        self.assertEqual(out["citations"], [])
        self.assertEqual(out["invalid_citations"], ["corpus/gl/imaginary.p#main"])


class TestMeter(unittest.TestCase):
    def test_meter_accumulates_and_serializes(self):
        m = Meter(model="claude-sonnet-4-5")
        m.add(1000, 200)
        m.add(500, 100, estimated=True)
        d = m.as_dict()
        self.assertEqual(d["calls"], 2)
        self.assertEqual(d["input_tokens"], 1500)
        self.assertEqual(d["output_tokens"], 300)
        self.assertTrue(d["tokens_estimated"])
        self.assertGreater(d["estimated_cost_usd"], 0)

    def test_fake_llm_records_prompts_and_usage(self):
        fake = FakeLLM(["hi"])
        fake.complete("sys", "user text")
        self.assertEqual(fake.prompts, [("sys", "user text")])
        self.assertEqual(fake.meter.calls, 1)

    def test_approx_tokens_positive(self):
        self.assertGreaterEqual(approx_tokens(""), 1)
        self.assertEqual(approx_tokens("x" * 400), 100)


if __name__ == "__main__":
    unittest.main()
