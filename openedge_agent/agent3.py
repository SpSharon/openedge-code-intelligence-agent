"""Stage 3: the tool-using graph-walk agent.

Built exactly to docs/OPENEDGE_STAGE_3_DESIGN.md. `agent.py` stays frozen
as the Stage 2 baseline; this module reuses its `Retriever`, `build_context`,
`parse_response`, `REFUSAL_PHRASE` and `llm.py` unchanged, and exposes the
same public surface (`.retriever`, `.k`, `.build_prompt`, `.ask`) so
`evals/answer_score.py` runs as a drop-in via `--agent tool`.

The loop (design SS1): SEED -> DECIDE -> (ACT | NUDGE | FORCE) -> FINAL.
`llm.complete(system, user)` is stateless and stays that way: each step is
one `complete()` call whose `user` block is question + seed context + the
transcript of prior tool calls and results. No message-history API.

Citation discipline (design SS2) is structural, not just prompted:

    read_set  (citable)   <- seed context units + read_unit
    seen_ids  (NOT citable) <- walk_calls edges, search hits (no unit body)

`walk_calls` and `search` return no unit body (x-refs and call targets —
both source-derived — are included) and create no read-set entry, so only
a `read_unit` step (or the seed) can authorize a citation: `citations` is
a subset of `read_set` by construction. FINAL runs the same
`parse_response` funnel as Stage 2 with `allowed = list(read_set)`; anything
outside it lands in `invalid_citations`, never in `citations`. One guard
sits in front of the funnel: a refusal with no CITATIONS: line parses as
CITATIONS: none, never via the inline-[id] fallback.

Cost note: `evals/answer_score.py`'s `estimate_cost` prices the seed prompt,
i.e. a 1-step run. For this agent that is a LOWER BOUND — the honest
pre-spend figure is roughly that x steps. The `Meter` records the truth.

CLI (needs ANTHROPIC_API_KEY unless --fake):
    python -m openedge_agent.agent3 "which other programs does order entry run?"
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from collections import OrderedDict
from pathlib import Path

from .agent import (
    REFUSAL_PHRASE,
    SYSTEM_PROMPT,
    Agent,
    parse_response,
)
from .llm import AnthropicLLM, FakeLLM, make_llm

__all__ = ["ToolAgent", "REFUSAL_PHRASE"]

# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

# Stage 2 rules 1-5 are carried VERBATIM via SYSTEM_PROMPT; only additions
# below (design SS2a: rules 4b / 4c / 4d, then the tool protocol).

_CITATION_RULES_3 = """\

Additional citation rules (they extend rule 4; rule 5's format is unchanged):

4b. READ-SET RULE. Cite only units whose full text you have seen, in the \
retrieved context or via read_unit. A unit that a tool merely listed - a \
walk_calls edge, a search hit - is not readable evidence.
4c. NAME-THEN-READ. If your answer names a program or procedure as an answer \
to the question (not as passing context), read that unit and cite it. The \
unit holding the RUN statement is evidence that the call exists; the unit \
you name is evidence of what it is. Cite the ones your claim is about.
4d. READ-DOES-NOT-MEAN-CITE. A unit you read and did not use must not be \
cited. Typical answers cite 1-4 units.
"""

# The distinctive header below is asserted absent from the forced-final
# prompt by the tests; keep it stable.
_TOOL_MENU = """\

TOOLS AVAILABLE. You may call at most one tool per reply. To call a tool, \
reply with a single line starting with TOOL: (everything after that line is \
ignored):

TOOL: read_unit <unit-id>
    Full text of one unit (same text the retrieved context uses). Reading a \
unit adds it to your read set, making it citable.
TOOL: walk_calls <unit-id-or-file-path> <out|in|both>
    Call-graph edges for that unit (or every unit of that file). Returns \
ids and raw call targets, no unit body - walking to a unit never authorizes \
citing it.
TOOL: search <free-text query>
    Top-5 unit headers from the same retriever, different vocabulary welcome. \
Headers and x-refs only, no unit body - to cite a hit you must read it.

You have a budget of {max_steps} tool calls for this question. Stopping rules:
- ENOUGH TO ANSWER: every claim is supported by text you have read, every \
unit you intend to cite is in your read set, and the last tool call added no \
new evidence. Then reply with the answer and the final CITATIONS line - no \
TOOL line.
- KEEP WALKING: if your answer would name a program, procedure or table \
whose text you have not read, go read it first. If the question is "who \
calls X" or "what does X run" and you have seen only the RUN statement, \
read the target unit too.
- REFUSE: only after at least one search probe with different vocabulary \
than the question. If nothing you have read supports an answer, reply that \
the answer is "not in the retrieved code" and end with: CITATIONS: none
"""

SYSTEM_PROMPT_TOOL = SYSTEM_PROMPT + _CITATION_RULES_3 + _TOOL_MENU
SYSTEM_PROMPT_FORCE = SYSTEM_PROMPT + _CITATION_RULES_3

USER_TEMPLATE_3 = """\
Question: {question}

Retrieved code units ({n} units, in retrieval order; these are already in \
your read set):

{context}\
"""

_DECIDE_TAIL = (
    "Your move: reply with ONE TOOL: line, or with your final answer ending "
    "in the CITATIONS line."
)

_FORMAT_NUDGE = (
    "REMINDER (not a tool result): your last reply contained neither a "
    "TOOL: line nor a CITATIONS: line. Reply with exactly one TOOL: line, "
    "or with your final answer ending in the final CITATIONS line."
)

_REFUSAL_NUDGE = (
    "REMINDER (not a tool result): before refusing, run at least one "
    "TOOL: search probe with different vocabulary than the question. If "
    "nothing relevant turns up, refuse again and it will stand."
)

_TOOL_LINE_RE = re.compile(r"^\s*TOOL:\s*(.*)$")
_CITE_LINE_RE = re.compile(r"^\s*citations?\s*:", re.IGNORECASE)


def _normalize_reply(text: str) -> str:
    """Mirror of evals/answer_score._normalize: strip markdown emphasis /
    code ticks and collapse whitespace. Duplicated rather than imported —
    evals/answer_score.py imports this module, so importing it back would
    be circular. Keeps the loop's refusal detection in agreement with the
    frozen scorer, so a reply like "**not** in the `retrieved` code" is a
    refusal to both."""
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _is_refusal(reply: str) -> bool:
    return REFUSAL_PHRASE in _normalize_reply(reply).lower()


def _has_cite_line(reply: str) -> bool:
    return any(_CITE_LINE_RE.match(line) for line in reply.splitlines())

_CALL_TYPES = ("run_external", "run_internal", "run_in_handle", "function_call")
_DIRECTIONS = ("out", "in", "both")


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


class ToolAgent(Agent):
    """Tool-using graph-walk agent (Stage 3). Same public surface as Agent."""

    def __init__(self, index_dir: str | Path = "index", llm=None, k: int = 10,
                 max_answer_tokens: int = 700, max_steps: int = 6):
        super().__init__(index_dir=index_dir, llm=llm, k=k,
                         max_answer_tokens=max_answer_tokens)
        self.index_dir = Path(index_dir)
        self.max_steps = max_steps
        self.callgraph = json.loads(
            (self.index_dir / "callgraph.json").read_text()
        )
        # id-resolution maps (Stage 2 rules: exact -> case-insensitive ->
        # restore missing corpus/ prefix; bare file -> #main when it exists)
        self._ids_lower = {u["id"].lower(): u["id"]
                           for u in self.retriever.units}
        self._file_units: dict[str, list[str]] = {}
        for u in self.retriever.units:
            if u["id"].startswith("corpus/"):
                self._file_units.setdefault(u["file"], []).append(u["id"])
        for us in self._file_units.values():
            us.sort()
        self._files_lower = {f.lower(): f for f in self._file_units}
        # citable state; reset per ask(). seen_ids is bookkeeping only -
        # nothing in it is ever citable (design SS2b).
        self.read_set: OrderedDict[str, dict] = OrderedDict()
        self.seen_ids: set[str] = set()

    # -- id resolution ------------------------------------------------------

    def _resolve_candidates(self, raw: str) -> list[str]:
        """Raw string -> candidate strings to try, in order."""
        rid = raw.strip().strip(".,;:").strip("[](){}").strip(".,;:")
        cands = [rid]
        if not rid.lower().startswith(("corpus/", "schema:")):
            cands.append("corpus/" + rid)
        return cands

    def _resolve_unit_id(self, raw: str) -> str | None:
        """Resolve raw to a single existing unit id, or None."""
        for cand in self._resolve_candidates(raw):
            hit = self._ids_lower.get(cand.lower())
            if hit is not None:
                return hit
        return None

    def _resolve_file(self, raw: str) -> str | None:
        """Resolve raw to a known corpus file path, or None."""
        for cand in self._resolve_candidates(raw):
            hit = self._files_lower.get(cand.lower())
            if hit is not None:
                return hit
        return None

    def _did_you_mean(self, raw: str) -> list[str]:
        universe = list(self._ids_lower.values()) + list(self._file_units)
        return difflib.get_close_matches(raw, universe, n=5, cutoff=0.3)

    def _error(self, message: str, raw: str = "") -> dict:
        out = {"ok": False, "error": message}
        if raw:
            out["did_you_mean"] = self._did_you_mean(raw)
        return out

    # -- tools (design SS3). Failure: {ok: False, error, did_you_mean};
    # -- charged a step by the loop, no mutation of read_set. --------------

    def tool_read_unit(self, unit_id: str, step: int = 0) -> dict:
        uid = self._resolve_unit_id(unit_id)
        if uid is None:
            f = self._resolve_file(unit_id)
            if f is not None:
                main = f + "#main"
                if main in self._ids_lower.values():
                    uid = main
                else:
                    return {"ok": False,
                            "error": f"{f} has no #main unit; give a unit id",
                            "did_you_mean": self._file_units[f]}
            else:
                return self._error(f"unknown unit id: {unit_id!r}", unit_id)
        if uid in self.read_set:
            return {"ok": True, "unit_id": uid,
                    "note": "already in your context, "
                            f"step {self.read_set[uid]['step']}"}
        u = self._unit(uid)
        self.read_set[uid] = {"source": "read_unit", "step": step,
                              "chars": len(u["text"])}
        return {
            "ok": True,
            "unit_id": uid,
            "kind": u["kind"],
            "name": u.get("name", ""),
            "file": u["file"],
            "text": u["text"],
            "xref": u.get("xref", ""),
            "chars": len(u["text"]),
        }

    def _norm_target(self, edge: dict) -> str:
        """Mirror ingest.py's normalization: run_external targets enter the
        file's #main unit when that unit exists; other call types are
        already unit ids."""
        target = edge["resolved"]
        if edge["type"] == "run_external":
            cand = f"{target}#main"
            if cand.lower() in self._ids_lower:
                return self._ids_lower[cand.lower()]
        return target

    @staticmethod
    def _edge_extras(edge: dict) -> dict:
        extras = {}
        if "persistent" in edge:
            extras["persistent"] = edge["persistent"]
        if "handle" in edge:
            extras["handle"] = edge["handle"]
        return extras

    def tool_walk_calls(self, unit_id: str, direction: str) -> dict:
        if direction not in _DIRECTIONS:
            return {"ok": False,
                    "error": f"bad direction {direction!r}; "
                             f"allowed: {', '.join(_DIRECTIONS)}"}
        uid = self._resolve_unit_id(unit_id)
        if uid is not None:
            unit_set, label = {uid}, uid
        else:
            f = self._resolve_file(unit_id)
            if f is None:
                return self._error(f"unknown unit id or file: {unit_id!r}",
                                   unit_id)
            unit_set, label = set(self._file_units[f]), f

        calls_out, called_by, includes, included_by = [], [], [], []
        for e in self.callgraph["edges"]:
            if not e.get("resolved"):
                continue
            if e["type"] == "include":
                if direction in ("out", "both") and e["from"] in unit_set:
                    includes.append({"unit_id": e["resolved"],
                                     "from_unit": e["from"],
                                     "target_raw": e.get("target_raw")})
                if direction in ("in", "both") and e["resolved"] in unit_set:
                    included_by.append({"unit_id": e["from"],
                                        "target_raw": e.get("target_raw")})
            elif e["type"] in _CALL_TYPES:
                target = self._norm_target(e)
                if direction in ("out", "both") and e["from"] in unit_set:
                    calls_out.append({"unit_id": target, "type": e["type"],
                                      "target_raw": e.get("target_raw"),
                                      "from_unit": e["from"],
                                      **self._edge_extras(e)})
                if direction in ("in", "both") and target in unit_set:
                    called_by.append({"unit_id": e["from"], "type": e["type"],
                                      "target_raw": e.get("target_raw"),
                                      "target_unit": target,
                                      **self._edge_extras(e)})

        # Unresolved edges: those leaving the walked set, plus every dynamic
        # RUN VALUE edge in the corpus (any call list may be incomplete
        # because of it - design SS3: never imply completeness).
        unresolved = []
        for e in self.callgraph["edges"]:
            if e.get("resolved"):
                continue
            if e["type"] == "run_dynamic":
                unresolved.append({"from_unit": e["from"], "type": e["type"],
                                   "target_raw": e.get("target_raw"),
                                   "reason": "target not statically known"})
            elif e["from"] in unit_set:
                unresolved.append({"from_unit": e["from"], "type": e["type"],
                                   "target_raw": e.get("target_raw"),
                                   "reason": "target not found in corpus"})

        for lst in (calls_out, called_by, includes, included_by, unresolved):
            lst.sort(key=lambda d: (d.get("unit_id", ""),
                                    d.get("from_unit", ""),
                                    d.get("type", "")))
        result = {
            "ok": True,
            "unit_id": label,
            "direction": direction,
            "calls_out": calls_out,
            "called_by": called_by,
            "includes": includes,
            "included_by": included_by,
            "unresolved": unresolved,
            "note": "ids and raw call targets, no unit body; to cite a "
                    "unit you must read_unit it. Call lists may be "
                    "incomplete: dynamic RUN VALUE targets are never "
                    "statically resolved.",
        }
        for lst in (calls_out, called_by):
            self.seen_ids.update(d["unit_id"] for d in lst)
        return result

    def tool_search(self, query: str) -> dict:
        q = query.strip()
        if not q:
            return {"ok": False, "error": "empty search query"}
        if len(q) > 200:
            return {"ok": False,
                    "error": f"query too long ({len(q)} chars; max 200)"}
        hits = []
        for h in self.retriever.search(q, k=5):
            u = self._unit(h["id"])
            hits.append({
                "unit_id": u["id"],
                "kind": u["kind"],
                "name": u.get("name", ""),
                "file": u["file"],
                "header": self._unit_header(u),
                "xref": u.get("xref", ""),
                "score": round(h["score"], 4),
                "already_read": u["id"] in self.read_set,
            })
        self.seen_ids.update(h["unit_id"] for h in hits)
        return {"ok": True, "query": q, "hits": hits,
                "note": "headers and x-refs only, no unit body; to cite a "
                        "hit you must read_unit it."}

    # -- reply classification / tool-line parsing ---------------------------

    @staticmethod
    def _classify(reply: str) -> tuple[str, str | None]:
        """-> ("tool", payload) | ("final", None) | ("none", None).

        A reply is a tool call only when its TOOL: line leads: either the
        TOOL: line is the first non-blank line (the protocol form —
        everything after it is ignored), or the reply has no CITATIONS:
        line at all (a genuine call preceded by prose). A reply that
        carries a CITATIONS: line and merely quotes a line-start TOOL:
        inside the answer body is FINAL — it must not be re-dispatched
        (adversarial-review D2). The first TOOL: line wins as payload."""
        payload = None
        tool_leads = False
        saw_nonblank = False
        saw_cite = False
        for line in reply.splitlines():
            m = _TOOL_LINE_RE.match(line)
            if m and payload is None:
                payload = m.group(1)
                if not saw_nonblank:
                    tool_leads = True
            if _CITE_LINE_RE.match(line):
                saw_cite = True
            if line.strip():
                saw_nonblank = True
        if payload is not None and (tool_leads or not saw_cite):
            return "tool", payload
        if saw_cite:
            return "final", None
        return "none", None

    def _dispatch_tool(self, payload: str, step: int) -> tuple[str, dict]:
        """Parse 'name args...' (whitespace-delimited) and run the tool."""
        parts = payload.split(None, 1)
        name = parts[0] if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if name == "read_unit":
            args = rest.split()
            if len(args) != 1:
                return name, {"ok": False,
                              "error": "usage: TOOL: read_unit <unit-id>"}
            return name, self.tool_read_unit(args[0], step=step)
        if name == "walk_calls":
            args = rest.split()
            if len(args) != 2:
                return name, {"ok": False,
                              "error": "usage: TOOL: walk_calls "
                                       "<unit-id-or-file-path> <out|in|both>"}
            return name, self.tool_walk_calls(args[0], args[1])
        if name == "search":
            return name, self.tool_search(rest)
        return name, {"ok": False,
                      "error": f"unknown tool {name!r}; available: "
                               "read_unit, walk_calls, search"}

    # -- prompt assembly ----------------------------------------------------

    def build_prompt(self, question: str, retrieved_ids: list[str]) -> tuple[str, str]:
        """The SEED prompt (first DECIDE call, empty transcript). NOTE for
        cost estimates: this prices a 1-step run - a lower bound for a
        multi-step agent."""
        context = self.build_context(retrieved_ids)
        user = USER_TEMPLATE_3.format(question=question,
                                      n=len(retrieved_ids), context=context)
        return SYSTEM_PROMPT_TOOL, user

    def _compose_user(self, seed_user: str, transcript: list[str],
                      force: bool) -> str:
        blocks = [seed_user, *transcript]
        if force:
            listing = "\n".join(
                f"- {uid}  (source: {meta['source']}, step {meta['step']}, "
                f"{meta['chars']} chars)"
                for uid, meta in self.read_set.items()
            )
            blocks.append(
                "TOOL BUDGET EXHAUSTED - FINAL ANSWER REQUIRED. No more "
                "tool calls. You have read these units (read does not mean "
                "cite: cite only the ones your answer actually draws on):\n"
                f"{listing}\n"
                "Answer now from what you have read, or reply that the "
                'answer is "not in the retrieved code" with CITATIONS: '
                "none. End with the final CITATIONS line."
            )
        else:
            blocks.append(_DECIDE_TAIL)
        return "\n\n".join(blocks)

    # -- the loop (design SS1) ---------------------------------------------

    def ask(self, question: str) -> dict:
        # SEED
        hits = self.retriever.search(question, k=self.k)
        retrieved_ids = [h["id"] for h in hits]
        self.read_set = OrderedDict()
        self.seen_ids = set()
        for uid in retrieved_ids:
            u = self._unit(uid)
            self.read_set[uid] = {"source": "seed", "step": 0,
                                  "chars": len(u["text"])}
        system, seed_user = self.build_prompt(question, retrieved_ids)

        transcript: list[str] = []
        trace: list[dict] = []
        steps = 0
        total_nudges = 0        # hard cap 2 (=> <= 9 LLM calls total)
        consec_nudges = 0
        consec_tool_errors = 0
        n_search_probes = 0
        refusal_nudged = False
        forced = False
        reply = ""

        while True:
            if steps >= self.max_steps:
                forced = True
                break
            user = self._compose_user(seed_user, transcript, force=False)
            reply = self.llm.complete(system, user,
                                      max_tokens=self.max_answer_tokens)
            kind, payload = self._classify(reply)

            if kind == "tool":
                steps += 1
                consec_nudges = 0
                name, result = self._dispatch_tool(payload, step=steps)
                if result.get("ok"):
                    consec_tool_errors = 0
                    if name == "search":
                        n_search_probes += 1
                else:
                    consec_tool_errors += 1
                transcript.append(
                    f"STEP {steps} - your tool call: TOOL: {payload}\n"
                    f"TOOL RESULT (step {steps}):\n"
                    + json.dumps(result, indent=1)
                )
                trace.append({"action": "tool", "step": steps, "tool": name,
                              "call": payload, "ok": bool(result.get("ok"))})
                if consec_tool_errors >= 2:
                    forced = True
                    break
                continue

            if kind == "final":
                # normalize markdown first so the nudge gate detects the
                # same refusals the frozen scorer does ("**not** in the
                # `retrieved` code" is a refusal to both).
                is_refusal = _is_refusal(reply)
                if (is_refusal and n_search_probes == 0
                        and not refusal_nudged and total_nudges < 2):
                    # single soft nudge, never a loop (design SS1 refusal rule)
                    refusal_nudged = True
                    total_nudges += 1
                    consec_nudges += 1
                    transcript.append(_REFUSAL_NUDGE)
                    trace.append({"action": "nudge", "kind": "refusal-probe"})
                    continue
                trace.append({"action": "final"})
                break

            # kind == "none" -> format nudge
            if consec_nudges >= 2 or total_nudges >= 2:
                forced = True
                break
            total_nudges += 1
            consec_nudges += 1
            transcript.append(_FORMAT_NUDGE)
            trace.append({"action": "nudge", "kind": "format"})
            continue

        if forced:
            user = self._compose_user(seed_user, transcript, force=True)
            reply = self.llm.complete(SYSTEM_PROMPT_FORCE, user,
                                      max_tokens=self.max_answer_tokens)
            trace.append({"action": "force"})

        # C1 guard: a refusal must never emit a citation. parse_response's
        # inline-[id] fallback fires whenever a reply has no CITATIONS:
        # line, so a refusal that omitted the line would emit its inline
        # [id] mentions as citations. Treat such a reply as CITATIONS:
        # none before parsing (raw_reply keeps the model's actual text).
        # Covers the forced-final path and any final that reaches parsing
        # without a CITATIONS: line; the frozen parse_response is untouched.
        to_parse = reply
        if _is_refusal(reply) and not _has_cite_line(reply):
            to_parse = reply + "\nCITATIONS: none"

        # FINAL: same validated funnel as Stage 2, allowed = the read set.
        parsed = parse_response(to_parse, list(self.read_set))
        return {
            # Stage 2 keys, byte-identical semantics; `retrieved` stays the
            # seed top-10 so gold_in_context_rate is Stage-2-comparable.
            "answer": parsed["answer"],
            "citations": parsed["citations"],
            "invalid_citations": parsed["invalid_citations"],
            "retrieved": retrieved_ids,
            "raw_reply": reply,
            # new keys; the frozen scorer ignores them
            "read": {uid: dict(meta) for uid, meta in self.read_set.items()},
            "steps": steps,
            "trace": trace,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Ask the Stage 3 tool agent")
    ap.add_argument("question")
    ap.add_argument("--index", default="index")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--fake", action="store_true",
                    help="offline plumbing demo with FakeLLM (scripted: one "
                         "walk of the top unit's file, then a seed citation)")
    args = ap.parse_args()

    if args.fake:
        state = {"n": 0}

        def scripted(system: str, user: str) -> str:
            state["n"] += 1
            first = user.split("[", 1)[1].split("]", 1)[0]
            if state["n"] == 1 and first.startswith("corpus/"):
                return f"TOOL: walk_calls {first.split('#')[0]} both"
            return ("FAKE offline demo answer - drawn from the top retrieved "
                    f"unit [{first}].\nCITATIONS: {first}")
        llm = FakeLLM(scripted)
    else:
        llm = make_llm()

    agent = ToolAgent(index_dir=args.index, llm=llm, k=args.k,
                      max_steps=args.max_steps)
    out = agent.ask(args.question)
    print(out["answer"])
    print()
    print("citations :", "; ".join(out["citations"]) or "(none)")
    if out["invalid_citations"]:
        print("INVALID   :", "; ".join(out["invalid_citations"]))
    print("steps     :", out["steps"])
    print("read      :", "; ".join(
        uid for uid, m in out["read"].items() if m["source"] == "read_unit"
    ) or "(seed only)")
    print("usage     :", agent.llm.meter.as_dict())


if __name__ == "__main__":
    main()
