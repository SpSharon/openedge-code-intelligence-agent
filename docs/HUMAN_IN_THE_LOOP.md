# Human-in-the-Loop and Integrity Controls

Written in the form a model-risk reviewer reads: **obligation → control → evidence → owner**. Every row
points at code or a test in this repository, never at a description of one.

This is a personal learning project. It does not run in production, and **the corpus is synthetic** — no
employer or client code, schema, or business logic appears anywhere in it.

## The controls

| Obligation | Control | Evidence | Owner |
|---|---|---|---|
| An answer may cite only code the agent actually **read** | The citable set (`read_set`) is kept separate from what was merely *seen* (`seen_ids`). Walking the call graph or searching returns no code body, so neither can authorise a citation. Citations are validated against the read set at parse time | `openedge_agent/agent3.py` — `read_set` / `seen_ids`; `openedge_agent/agent.py` — `parse_response(text, allowed)` | Enforced by construction |
| The guarantee must survive a hostile model, not a cooperative one | **1,500 adversarial reply streams.** The test asserts zero violations of *citations ⊆ read-set*, and separately asserts that more than half the streams actually attempted a forbidden citation — so a passing run cannot mean the attacks never happened | `tests/test_agent3_fuzz.py` — `N_STREAMS = 1500` | Enforced by construction |
| The agent refuses rather than guesses | When the retrieved code does not contain the answer, it says so: `"not in the retrieved code"` | `openedge_agent/agent.py` — `REFUSAL_PHRASE`; `tests/test_agent.py` | Enforced by construction |
| Autonomy is bounded, and the bound is stated to the model | A budget of **six tool executions** per question, with explicit stopping rules in the prompt and enforced by the loop | `openedge_agent/agent3.py` — `max_steps = 6` | Set deliberately, not by default |
| Published metrics must be re-derivable by someone who does not trust the author | `python evals/answer_score.py --rescore <scoreboard.json>` re-derives every stored metric from the archived answers — **no API key, no network** | `evals/answer_score.py`; the archived scoreboards in `evals/results/` | Anyone |

## What these controls do not do

| Limit | Why it is stated |
|---|---|
| The guarantee is about **citation integrity, not correctness** | A citation being earned does not make the answer right. Answer quality is measured separately, by the evaluation harness |
| `--rescore` proves the metrics, not the answers | It shows the published numbers follow from the archived answers. It says nothing about whether those answers were good — the README states the same boundary |
| Nothing here has been audited by a third party | The evidence offered is the code, the tests, and the archived scoreboards |
| The system does not run in production | No live users, no incident history, no operational controls beyond what is in this repository |

## Checking any row

```
python -m unittest discover -s tests          # offline; numpy is the only dependency
python evals/answer_score.py --rescore evals/results/answers_scoreboard_stage3_heldout_r1.json
```

*Method: the system was specified and verified by the author; AI coding agents wrote the implementation.*
