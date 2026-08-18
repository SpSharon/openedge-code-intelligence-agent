"""Randomized adversarial fuzz for the Stage 3 read-set invariant
(README: `citations ⊆ read_set` by construction, not by instruction).

Each stream scripts a FakeLLM adversary against the real ToolAgent over
the real index. A stream may:

  - cite units it never read (random index ids outside the read set),
  - cite units it has only *seen* via walk_calls edges or search hits
    (the bookkeeping-only `seen_ids` set — never citable by design),
  - cite malformed / nonexistent / mangled ids, bare file paths,
    eval-case ids from the scoring stage (C2, H9, ...), and duplicates,
  - omit the CITATIONS line so the inline-[id] fallback fires,
  - refuse while carrying citations (the C1 guard path),
  - overrun the tool budget into the forced-final path, issue bad tool
    calls, or reply garbage to draw format nudges.

After every stream the invariant is checked:

    set(out["citations"]) <= set(out["read"])    # seed + read_unit only

and every surviving citation's read-set entry must have source "seed"
or "read_unit" — nothing enters via walk_calls or search.

Deterministic: one fixed-seed random.Random drives everything in a
single thread, so `python -m unittest tests.test_agent3_fuzz`
reproduces stream-for-stream. On completion the actual stream and
violation counts are printed to stderr so the README figure can be
checked against a real run. Needs index/ (run
`python -m openedge_agent.ingest` once), like every agent3 test.
"""

import random
import sys
import unittest
from pathlib import Path

from openedge_agent.agent3 import ToolAgent
from openedge_agent.llm import FakeLLM

ROOT = Path(__file__).resolve().parent.parent

SEED = 20260817
N_STREAMS = 1500

QUESTIONS = [
    "Which other programs does order entry run?",
    "What happens when an order is posted?",
    "Where is the credit limit checked?",
    "Which tables does invoice posting write?",
    "How are backorders reported?",
]

# Ids that must never authorize a citation: malformed, nonexistent,
# eval-case ids from the scoring stage, paths outside the unit-id space.
MALFORMED = [
    "", "none", "corpus/", "#main", "corpus/oe/", "oe-entry.p#",
    "corpus/oe/oe-entry.p#main#main",
    "corpus/../corpus/oe/oe-entry.p#main",
    "corpus/oe/does-not-exist.p#main", "schema:NoSuchTable",
    "C2", "H9", "S1",
    "evals/answers.jsonl", "index/chunks.json",
    "corpus/oe/oe entry.p#main", "corpus\\oe\\oe-entry.p#main",
    "[corpus/oe/oe-entry.p#main]", "corpus/oe/oe-entry.p#main.",
    "TOOL: read_unit corpus/oe/oe-entry.p#main",
    "corpus/oe/oe-credıt.p#main",   # dotless-i homoglyph
]

SEARCHES = [
    "credit limit", "post invoice status", "backorder report",
    "zz nothing matches this", "order status include", "", "x" * 250,
]


def _mangle(rng, uid):
    r = rng.random()
    if r < 0.25:
        return uid.upper()
    if r < 0.5:
        return uid.replace("corpus/", "", 1)
    if r < 0.75:
        return uid.split("#")[0]            # bare file path
    return " " + uid + " "


class FuzzReadSetInvariant(unittest.TestCase):
    """citations ⊆ read_set must survive N_STREAMS adversarial streams."""

    @classmethod
    def setUpClass(cls):
        cls.agent = ToolAgent(index_dir="index", llm=FakeLLM(), k=10)
        cls.all_ids = [u["id"] for u in cls.agent.retriever.units]
        cls.files = sorted({u["file"] for u in cls.agent.retriever.units
                            if u["id"].startswith("corpus/")})

    # -- adversarial reply builders (live agent state, rng-driven) ---------

    def _tool_line(self, rng):
        r = rng.random()
        if r < 0.40:
            if rng.random() < 0.55:
                target = rng.choice(self.all_ids)
            elif rng.random() < 0.5:
                target = _mangle(rng, rng.choice(self.all_ids))
            else:
                target = rng.choice(MALFORMED) or "x"
            return "TOOL: read_unit " + target
        if r < 0.70:
            target = rng.choice(self.files + self.all_ids)
            direction = rng.choice(["out", "in", "both", "sideways", ""])
            return f"TOOL: walk_calls {target} {direction}".rstrip()
        if r < 0.90:
            return "TOOL: search " + rng.choice(SEARCHES)
        return rng.choice([
            "I think the answer is oe-entry.\n\nLet me keep looking.",
            "TOOL: teleport corpus/oe/oe-entry.p#main",
            "TOOL: read_unit",
        ])

    def _final_reply(self, rng):
        agent = self.agent
        read = list(agent.read_set)
        seen_unread = sorted(u for u in agent.seen_ids
                             if u not in agent.read_set)
        unread = [u for u in self.all_ids if u not in agent.read_set]
        cands = []
        for _ in range(rng.randint(1, 8)):
            r = rng.random()
            if r < 0.30 and seen_unread:
                cands.append(rng.choice(seen_unread))   # walked/searched only
            elif r < 0.55 and unread:
                cands.append(rng.choice(unread))        # never seen at all
            elif r < 0.70 and read:
                cands.append(rng.choice(read))          # legitimately citable
            elif r < 0.85:
                cands.append(rng.choice(MALFORMED))
            else:
                cands.append(_mangle(rng, rng.choice(read or self.all_ids)))
        if cands and rng.random() < 0.4:
            cands.append(rng.choice(cands))             # duplicates
        sep = "; " if rng.random() < 0.7 else ", "
        style = rng.random()
        if style < 0.60:
            return "Adversarial answer.\nCITATIONS: " + sep.join(cands)
        if style < 0.75:    # refusal that still carries a CITATIONS line
            return ("The answer is not in the retrieved code.\n"
                    "CITATIONS: " + sep.join(cands))
        if style < 0.90:    # no CITATIONS line -> inline-[id] fallback fires
            body = " and ".join(f"[{c}]" for c in cands) or "[nothing]"
            return f"The relevant units are {body}."
        # refusal, no CITATIONS line, inline ids (the C1 guard path)
        body = " ".join(f"[{c}]" for c in cands)
        return f"**not** in the retrieved code {body}"

    # -- the fuzz -----------------------------------------------------------

    def test_citations_subset_of_read_set_under_adversarial_streams(self):
        rng = random.Random(SEED)
        violations = []
        n_attacked = 0      # streams whose reply tried a non-citable id
        for i in range(N_STREAMS):
            n_tools = rng.randint(0, 8)   # sometimes over budget -> force
            state = {"n": 0}

            def reply(system, user, _state=state, _n=n_tools):
                _state["n"] += 1
                if _state["n"] <= _n:
                    return self._tool_line(rng)
                return self._final_reply(rng)

            self.agent.llm = FakeLLM(reply)
            out = self.agent.ask(QUESTIONS[i % len(QUESTIONS)])

            cited, readset = set(out["citations"]), set(out["read"])
            if not cited <= readset:
                violations.append((i, sorted(cited - readset)))
            if out["invalid_citations"]:
                n_attacked += 1
            for uid in out["citations"]:
                self.assertIn(out["read"][uid]["source"],
                              ("seed", "read_unit"),
                              msg=f"stream {i}: {uid} entered the citable "
                                  "set via a non-authorizing source")

        sys.stderr.write(
            f"\n[fuzz] {N_STREAMS} adversarial reply streams, "
            f"{len(violations)} violations of citations <= read_set "
            f"({n_attacked} streams attempted a non-citable id and were "
            f"filtered)\n")
        # vacuity guard: the fuzz is meaningless if the adversary never
        # actually attempts an out-of-read-set citation
        self.assertGreater(n_attacked, N_STREAMS // 2)
        self.assertEqual(violations, [],
                         msg="read-set invariant violated: "
                             f"{violations[:5]} ...")


if __name__ == "__main__":
    unittest.main()
