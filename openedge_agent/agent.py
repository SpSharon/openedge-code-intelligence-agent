"""Stage 2: the citation-bearing knowledge agent.

Single-shot retrieval-augmented answering over the Stage 1 index — no
agent loop, no tools (that's Stage 3). Flow:

    question -> Retriever.search(k=10) -> grounded context block
             -> ONE LLM call -> {answer, citations}

Grounding contract (enforced by the prompt, checked by the parser and
the eval): the answer comes only from the retrieved units; citations
are the retrieved unit ids that actually support the answer; anything
the retrieval didn't surface is answered with "not in the retrieved
code", never invented. Citations the model emits that were NOT in the
retrieved set are stripped from `citations` and surfaced in
`invalid_citations` — hallucinated cites are made visible, not silently
cleaned.

Usage:
    from openedge_agent.agent import Agent
    from openedge_agent.llm import AnthropicLLM, FakeLLM

    agent = Agent(index_dir="index", llm=AnthropicLLM())
    out = agent.ask("What happens when an order is posted?")
    out["answer"], out["citations"]

CLI (needs ANTHROPIC_API_KEY unless --fake):
    python -m openedge_agent.agent "what calls ar-invoice.p?"
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from .llm import AnthropicLLM, FakeLLM, make_llm
from .retrieve import Retriever

REFUSAL_PHRASE = "not in the retrieved code"

# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a code-intelligence assistant for a legacy Progress OpenEdge (ABL/4GL) \
order-management codebase. You answer developer questions using ONLY the \
retrieved code units provided in the user message. Each unit is labelled with \
its unit id in square brackets, e.g. [corpus/oe/oe-post.p#main] or \
[schema:Customer].

Follow every rule exactly:

1. GROUNDING. Answer only from the text of the retrieved units. Do not use \
outside knowledge of ABL applications or typical order systems. Never invent \
or assume a procedure, table, field, status value, or behavior that does not \
appear in the retrieved text.
2. MISSING ANSWERS. If the retrieved units do not contain the answer, reply \
that the answer is "not in the retrieved code" and cite nothing. If they \
contain only part of the answer, answer that part and say the rest is not in \
the retrieved code.
3. STYLE. Be short and concrete: typically 2-6 sentences, naming the exact \
procedures, tables, fields, and status values involved. When you name one \
inline, you may reference its unit id in square brackets.
4. CITATIONS. Cite exactly the unit ids whose text your answer actually draws \
on — every unit you used, and none you did not use. A unit that was retrieved \
but contributed nothing to the answer must not be cited. Use ids exactly as \
given, including the corpus/ or schema: prefix and any #procedure suffix.
5. FORMAT. End your reply with a single final line, nothing after it:
CITATIONS: <unit id>; <unit id>; ...
If no unit supports an answer (rule 2), end with exactly:
CITATIONS: none
"""

USER_TEMPLATE = """\
Question: {question}

Retrieved code units ({n} units, in retrieval order):

{context}

Answer the question from these units only, then give the CITATIONS line.\
"""


# ---------------------------------------------------------------------------
# citation parsing
# ---------------------------------------------------------------------------

_CITE_LINE_RE = re.compile(r"^\s*citations?\s*:\s*(.*)$", re.IGNORECASE)
_INLINE_ID_RE = re.compile(r"\[((?:corpus/|schema:)[^\[\]\s]+)\]")


def _clean_id(raw: str) -> str:
    return raw.strip().strip(".,;: ").strip("[](){}").strip(".,;: ")


def parse_response(text: str, retrieved_ids: list[str]) -> dict:
    """Split an LLM reply into answer text + validated citations.

    Primary channel: the last 'CITATIONS: ...' line (semicolon- or
    comma-separated). Fallback: inline [unit-id] references in the answer
    body. Every candidate is validated against the retrieved set —
    exact, case-insensitive, or with a missing 'corpus/' prefix
    restored. Anything that doesn't resolve lands in invalid_citations.
    """
    lines = text.splitlines()
    cite_idx, cite_payload = None, None
    for i in range(len(lines) - 1, -1, -1):
        m = _CITE_LINE_RE.match(lines[i])
        if m:
            cite_idx, cite_payload = i, m.group(1)
            break

    if cite_idx is not None:
        answer = "\n".join(lines[:cite_idx] + lines[cite_idx + 1:]).strip()
        raw = [] if cite_payload.strip().lower() in ("none", "-", "") else [
            _clean_id(p) for p in re.split(r"[;,]", cite_payload)
        ]
    else:
        answer = text.strip()
        raw = [_clean_id(p) for p in _INLINE_ID_RE.findall(text)]

    by_lower = {rid.lower(): rid for rid in retrieved_ids}
    citations: list[str] = []
    invalid: list[str] = []
    for cand in raw:
        if not cand:
            continue
        resolved = by_lower.get(cand.lower())
        if resolved is None and not cand.lower().startswith(("corpus/", "schema:")):
            resolved = by_lower.get(("corpus/" + cand).lower())
        if resolved is None:
            invalid.append(cand)
        elif resolved not in citations:
            citations.append(resolved)
    return {"answer": answer, "citations": citations, "invalid_citations": invalid}


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


class Agent:
    def __init__(self, index_dir: str | Path = "index", llm=None, k: int = 10,
                 max_answer_tokens: int = 700):
        self.retriever = Retriever(index_dir=index_dir)  # frozen Stage 1 defaults
        self.llm = llm if llm is not None else make_llm()
        self.k = k
        self.max_answer_tokens = max_answer_tokens

    # -- context assembly --

    def _unit(self, unit_id: str) -> dict:
        return self.retriever.units[self.retriever.id_to_pos[unit_id]]

    @staticmethod
    def _unit_header(u: dict) -> str:
        if u["id"].startswith("schema:"):
            return f"[{u['id']}] ({u['kind']} from the database schema)"
        if u["kind"] == "include":
            return f"[{u['id']}] (include file)"
        return f"[{u['id']}] ({u['kind']} {u['name']} in {u['file']})"

    def build_context(self, retrieved_ids: list[str]) -> str:
        blocks = []
        for uid in retrieved_ids:
            u = self._unit(uid)
            block = f"{self._unit_header(u)}\n{u['text']}"
            xref = u.get("xref", "")
            if xref:
                block += f"\n{xref}"
            blocks.append(block)
        return "\n\n---\n\n".join(blocks)

    def build_prompt(self, question: str, retrieved_ids: list[str]) -> tuple[str, str]:
        context = self.build_context(retrieved_ids)
        user = USER_TEMPLATE.format(
            question=question, n=len(retrieved_ids), context=context
        )
        return SYSTEM_PROMPT, user

    # -- the one call --

    def ask(self, question: str) -> dict:
        hits = self.retriever.search(question, k=self.k)
        retrieved_ids = [h["id"] for h in hits]
        system, user = self.build_prompt(question, retrieved_ids)
        reply = self.llm.complete(system, user, max_tokens=self.max_answer_tokens)
        parsed = parse_response(reply, retrieved_ids)
        return {
            "answer": parsed["answer"],
            "citations": parsed["citations"],
            "invalid_citations": parsed["invalid_citations"],
            "retrieved": retrieved_ids,
            "raw_reply": reply,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Ask the Stage 2 agent one question")
    ap.add_argument("question")
    ap.add_argument("--index", default="index")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--fake", action="store_true",
                    help="offline plumbing demo with FakeLLM (no API call; "
                         "the 'answer' is scripted, only retrieval/parsing "
                         "are real)")
    args = ap.parse_args()

    if args.fake:
        def scripted(system: str, user: str) -> str:
            first = user.split("[", 1)[1].split("]", 1)[0]
            return (
                "FAKE offline demo answer - drawn from the top retrieved "
                f"unit [{first}].\nCITATIONS: {first}"
            )
        llm = FakeLLM(scripted)
    else:
        llm = make_llm()

    agent = Agent(index_dir=args.index, llm=llm, k=args.k)
    out = agent.ask(args.question)
    print(out["answer"])
    print()
    print("citations :", "; ".join(out["citations"]) or "(none)")
    if out["invalid_citations"]:
        print("INVALID   :", "; ".join(out["invalid_citations"]))
    print("retrieved :", "; ".join(out["retrieved"]))
    print("usage     :", agent.llm.meter.as_dict())


if __name__ == "__main__":
    main()
