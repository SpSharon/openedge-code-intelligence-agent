"""The external-task worker for the governed step: topic `draft-answer`.

What it does, per task the engine hands it (`camunda_step/governed-answer.bpmn`):
read the question and the prompt version from the process variables, ask the
Stage 3 agent, and write back the answer, its citations, whether it refused,
the model id, the token counts, the prompt version it served, a hash of the
answer and the attempt number — the audit record the review form and Camunda's
history then carry. On a rejection the reviewer's note travels back with the
task and is appended to the question.

The three things the bid names, and how each is done here:

- Idempotency. The engine may hand the same task out twice (a lock that
  expired mid-call, a worker that completed and crashed before the engine heard
  it). Every completion is stored first, keyed by the external task id; a task
  seen before is completed from the store without a second model call.
- Retries with backoff. A transient failure (the model API unreachable, 429,
  5xx, a timeout) is reported with `retries` one lower than before and a
  `retryTimeout` that grows — retried twice, after 5 s and then 15 s — and at
  zero the engine raises an incident for a person in Cockpit. A permanent
  failure (bad credentials, a bug) is reported with `retries = 0` at once: an
  incident, not a retry storm.
- The lock. A model call can outlive a short lock; a keep-alive thread extends
  the lock while the call runs, so the engine does not hand the task to another
  worker in the meantime.

Limits, stated: the engine's String variables hold about 4,000 characters, so
the answer is truncated at 3,900 and flagged (`answer_truncated`), with its
SHA-256 over the full text; the full record is in the worker's own store. The
review form's generated fields are single-line inputs, so the worker also writes
`answer_preview` — the answer on one line, 600 characters at most — for the form;
`answer` itself is the audit copy. The worker is one process polling one topic —
no priority, no multi-topic, no long-polling tuning. Nothing is deployed; it runs
on a local engine.

    python -m camunda_step.worker                            # fake-demo: offline answers, one citation
    python -m camunda_step.worker --backend anthropic        # real answers (ANTHROPIC_API_KEY)
    python -m camunda_step.worker --once                     # one poll, then exit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from openedge_agent.agent3 import REFUSAL_PHRASE, ToolAgent, _normalize_reply
from openedge_agent.llm import BACKENDS, FakeLLM, make_llm

from .engine import DEFAULT_URL, Engine, EngineError, EngineTransient, plain, typed

TOPIC = "draft-answer"
PROMPT_VERSION_SERVED = "agent3-2026-07-26"   # the prompt in agent3.py, dated by its design
MAX_STRING = 3900                               # under the engine's ~4,000-character String column
DEFAULT_BACKOFF_MS = (5_000, 15_000)              # the waits before the 2nd and 3rd attempt
PREVIEW_CHARS = 600
TRANSIENT_NAMES = {
    # the Anthropic SDK
    "APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError",
    "ServiceUnavailableError", "OverloadedError",
    # botocore / Bedrock (modeled exceptions carry no status_code attribute)
    "ThrottlingException", "ServiceUnavailableException", "InternalServerException",
    "ModelTimeoutException", "ModelNotReadyException", "ReadTimeoutError", "ConnectTimeoutError",
    "EndpointConnectionError",
    # the standard library and this package
    "TimeoutError", "ConnectionError", "EngineTransient",
}
WORKER_BACKENDS = tuple(BACKENDS) + ("fake-demo",)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(event: str, **fields: Any) -> None:
    """One JSON line per event, to stdout — the worker's own log."""
    print(json.dumps({"ts": utcnow(), "event": event, **fields}, ensure_ascii=False), flush=True)


def is_transient(exc: BaseException) -> bool:
    """Retry what a wait can fix: throttling, 5xx, timeouts, a connection
    refused; never a bad key, a bad request or a bug."""
    name = type(exc).__name__
    if name in TRANSIENT_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if status is None:   # botocore ClientError keeps the HTTP code in the response metadata
        meta = getattr(exc, "response", None)
        if isinstance(meta, dict):
            status = (meta.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return isinstance(status, int) and (status == 429 or status >= 500)


def one_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    """The answer for a single-line form field: newlines marked, then capped."""
    flat = " ⏎ ".join(part.strip() for part in text.splitlines() if part.strip())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


def _scripted_demo(system: str, user: str) -> str:
    """The offline demo of agent3.main: cite the top retrieved unit."""
    first = user.split("[", 1)[1].split("]", 1)[0]
    return ("FAKE offline demo answer - drawn from the top retrieved unit "
            f"[{first}].\nCITATIONS: {first}")


def make_agent(backend: str, index_dir: str | Path = "index", max_steps: int = 6) -> ToolAgent:
    """A fresh agent per task, so the meter counts this task's tokens only."""
    if backend == "fake-demo":
        llm = FakeLLM(_scripted_demo)
    else:
        llm = make_llm(backend)
    return ToolAgent(index_dir=index_dir, llm=llm, max_steps=max_steps)


# ---------------------------------------------------------------------------
# the idempotent store
# ---------------------------------------------------------------------------


class Store:
    """Completed tasks by external task id: the idempotency key."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS completions (
            task_id TEXT PRIMARY KEY, instance_id TEXT, created_at TEXT NOT NULL,
            variables TEXT NOT NULL, full_answer TEXT NOT NULL)""")
        self.conn.commit()

    def get(self, task_id: str) -> dict | None:
        row = self.conn.execute("SELECT variables FROM completions WHERE task_id = ?",
                                (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, task_id: str, instance_id: str, variables: dict, full_answer: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO completions VALUES (?, ?, ?, ?, ?)",
                          (task_id, instance_id, utcnow(), json.dumps(variables), full_answer))
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM completions").fetchone()[0]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


class DraftWorker:
    def __init__(self, engine: Engine, agent_factory: Callable[[], ToolAgent], store: Store,
                 worker_id: str | None = None, topic: str = TOPIC, lock_ms: int = 60_000,
                 max_retries: int = 3, backoff_ms: tuple[int, ...] = DEFAULT_BACKOFF_MS,
                 max_string: int = MAX_STRING, long_poll_ms: int = 0, sleep=time.sleep):
        self.engine = engine
        self.agent_factory = agent_factory
        self.store = store
        self.worker_id = worker_id or f"draft-worker-{uuid.uuid4().hex[:8]}"
        self.topic = topic
        self.lock_ms = lock_ms
        self.max_retries = max_retries
        self.backoff_ms = backoff_ms
        self.max_string = max_string
        self.long_poll_ms = long_poll_ms
        self.sleep = sleep
        self.metrics = {"fetched": 0, "completed": 0, "duplicates": 0, "retries": 0,
                        "incidents": 0, "lock_extensions": 0}

    # -- one poll ----------------------------------------------------------------

    def run_once(self) -> list[str]:
        """Fetch up to one task and handle it. Returns the outcomes."""
        try:
            tasks = self.engine.fetch_and_lock(self.worker_id, self.topic, self.lock_ms,
                                               max_tasks=1, long_poll_ms=self.long_poll_ms)
        except EngineTransient as exc:
            log("engine_unreachable", error=str(exc))
            return ["engine_unreachable"]
        outcomes = []
        for task in tasks:
            self.metrics["fetched"] += 1
            try:
                outcomes.append(self.handle(task))
            except Exception as exc:  # noqa: BLE001 — one task must not end the poll loop
                log("handler_error", task_id=task.get("id"), error=f"{type(exc).__name__}: {exc}",
                    note="left locked; the engine re-delivers it when the lock expires")
                outcomes.append("handler_error")
        return outcomes

    def run(self, poll_ms: int = 2_000, max_polls: int | None = None) -> None:
        polls = 0
        while max_polls is None or polls < max_polls:
            polls += 1
            outcomes = self.run_once()
            if not outcomes or outcomes == ["engine_unreachable"]:
                self.sleep(poll_ms / 1000.0)

    # -- one task ------------------------------------------------------------------

    def handle(self, task: dict) -> str:
        task_id, instance_id = task["id"], task.get("processInstanceId", "")
        variables = plain(task.get("variables"))
        question = (variables.get("question") or "").strip()
        prompt_version = variables.get("prompt_version") or "unversioned"
        attempt = int(variables.get("draft_attempt") or 0) + 1

        # 1. idempotency: seen before -> complete from the store, no model call
        stored = self.store.get(task_id)
        if stored is not None:
            try:
                self.engine.complete(task_id, self.worker_id, stored)
            except EngineTransient as exc:
                log("duplicate_complete_transient", task_id=task_id, error=str(exc),
                    note="the engine will hand the task out again; the store answers again")
                return "complete_transient"
            except EngineError as exc:
                log("duplicate_complete_rejected", task_id=task_id, error=str(exc))
                return "lost_lock"
            self.metrics["duplicates"] += 1
            log("duplicate", task_id=task_id, instance=instance_id,
                note="completed from the store; the model was not called again")
            return "duplicate"

        if not question:
            return self._fail(task, "the task carries no question", "", permanent=True)

        # 2. the reviewer's note travels with a rejection
        note = (variables.get("reviewer_note") or "").strip()
        asked = question
        if attempt > 1 and note:
            asked = f"{question}\n\nThe reviewer rejected the previous draft with this note: {note}"

        # 3. the model call, with the lock kept alive while it runs
        started = time.monotonic()
        stop = threading.Event()
        keeper = threading.Thread(target=self._keep_lock, args=(task_id, stop), daemon=True)
        keeper.start()
        try:
            agent = self.agent_factory()
            result = agent.ask(asked)
            usage = agent.llm.meter.as_dict()
        except Exception as exc:  # noqa: BLE001 — classified below
            stop.set()
            keeper.join(timeout=5)
            return self._fail(task, f"{type(exc).__name__}: {exc}", repr(exc),
                              permanent=not is_transient(exc))
        finally:
            stop.set()
        keeper.join(timeout=5)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        # 4. the audit record, as typed variables
        full_answer = result["answer"]
        answer = full_answer if len(full_answer) <= self.max_string else full_answer[: self.max_string]
        out = {
            "answer": typed(answer),
            "answer_preview": typed(one_line(full_answer)),
            "answer_truncated": typed(len(full_answer) > self.max_string),
            "answer_sha256": typed(hashlib.sha256(full_answer.encode("utf-8")).hexdigest()),
            "citations": typed("; ".join(result["citations"]) or "none"),
            "invalid_citations": typed("; ".join(result.get("invalid_citations") or []) or "none"),
            "refused": typed(REFUSAL_PHRASE in _normalize_reply(full_answer).lower()),   # the agent's own contract
            "model_id": typed(usage["model"]),
            "input_tokens": typed(int(usage["input_tokens"])),
            "output_tokens": typed(int(usage["output_tokens"])),
            "tokens_estimated": typed(bool(usage.get("tokens_estimated"))),
            "agent_steps": typed(int(result.get("steps", 0))),
            "prompt_version": typed(prompt_version),
            "prompt_version_served": typed(PROMPT_VERSION_SERVED),
            "draft_attempt": typed(attempt),
            "drafted_at": typed(utcnow()),
            "drafted_by": typed(self.worker_id),
            "draft_ms": typed(elapsed_ms),
        }

        # 5. store first, then complete — a crash between the two is a duplicate next time, not a second call
        self.store.put(task_id, instance_id, out, full_answer)
        try:
            self.engine.complete(task_id, self.worker_id, out)
        except EngineTransient as exc:
            log("complete_transient", task_id=task_id, error=str(exc),
                note="stored; the engine will hand the task out again and the store answers")
            return "complete_transient"
        except EngineError as exc:
            log("complete_rejected", task_id=task_id, error=str(exc),
                note="the lock was lost or the task is gone; stored for the re-delivery")
            return "lost_lock"
        self.metrics["completed"] += 1
        log("completed", task_id=task_id, instance=instance_id, attempt=attempt,
            citations=result["citations"], refused=out["refused"]["value"],
            model=usage["model"], tokens=[usage["input_tokens"], usage["output_tokens"]],
            ms=elapsed_ms, truncated=out["answer_truncated"]["value"])
        return "completed"

    # -- failure reporting ----------------------------------------------------------

    def _fail(self, task: dict, message: str, details: str, permanent: bool) -> str:
        task_id = task["id"]
        left = task.get("retries")
        left = self.max_retries if left is None else int(left)
        if permanent:
            retries, timeout, outcome = 0, 0, "incident"
            self.metrics["incidents"] += 1
        else:
            retries = max(left - 1, 0)
            used = self.max_retries - retries - 1          # waits already spent
            timeout = (self.backoff_ms[min(max(used, 0), len(self.backoff_ms) - 1)]
                       if retries > 0 else 0)              # no wait before an incident
            outcome = "retry" if retries > 0 else "incident"
            self.metrics["retries" if retries > 0 else "incidents"] += 1
        try:
            self.engine.failure(task_id, self.worker_id, message, details, retries, timeout)
        except EngineError as exc:
            log("failure_rejected", task_id=task_id, error=str(exc))
            return "lost_lock"
        except EngineTransient as exc:
            log("failure_transient", task_id=task_id, error=str(exc),
                note="the lock will expire and the engine will hand the task out again")
            return "failure_transient"
        log(outcome, task_id=task_id, instance=task.get("processInstanceId", ""),
            error=message, retries_left=retries, retry_timeout_ms=timeout, permanent=permanent)
        return outcome

    # -- the lock keep-alive ---------------------------------------------------------

    def _keep_lock(self, task_id: str, stop: threading.Event) -> None:
        interval = max(self.lock_ms / 2000.0, 0.05)
        while not stop.wait(interval):
            try:
                self.engine.extend_lock(task_id, self.worker_id, self.lock_ms)
                self.metrics["lock_extensions"] += 1
                log("lock_extended", task_id=task_id, new_duration_ms=self.lock_ms)
            except (EngineError, EngineTransient) as exc:
                log("lock_extend_failed", task_id=task_id, error=str(exc))
                return


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="External-task worker for topic draft-answer")
    ap.add_argument("--engine", default=os.environ.get("CAMUNDA_URL", DEFAULT_URL))
    ap.add_argument("--backend", default=os.environ.get("OE_AGENT_BACKEND", "fake-demo"),
                    choices=WORKER_BACKENDS,
                    help="fake-demo answers offline with one citation; fake returns an empty reply "
                         "offline (CITATIONS: none); anthropic / bedrock make real, paid model calls")
    ap.add_argument("--index", default=os.environ.get("OE_AGENT_INDEX", "index"))
    ap.add_argument("--store", default=os.environ.get("CAMUNDA_STORE", "index/camunda_completions.sqlite"),
                    help="the idempotent store (SQLite); under index/, which is gitignored")
    ap.add_argument("--lock-ms", type=int, default=60_000)
    ap.add_argument("--poll-ms", type=int, default=2_000)
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    ap.add_argument("--worker-id", default=None)
    args = ap.parse_args(argv)

    if not (Path(args.index) / "chunks.json").exists():
        print(f"no index at {args.index} — run `python -m openedge_agent.ingest` first", file=sys.stderr)
        return 2
    if args.backend in ("anthropic", "bedrock"):
        try:
            llm = make_llm(args.backend)
            ensure = getattr(llm, "_ensure_client", None)
            if ensure is not None:
                ensure()             # the key, the model id, the SDK — checked before the first fetch
        except Exception as exc:  # noqa: BLE001
            print(f"backend not ready: {exc}", file=sys.stderr)
            return 2
    engine = Engine(args.engine)
    store = Store(args.store)
    worker = DraftWorker(engine, lambda: make_agent(args.backend, args.index), store,
                         worker_id=args.worker_id, lock_ms=args.lock_ms)
    try:
        log("worker_start", worker_id=worker.worker_id, engine=args.engine, topic=TOPIC,
            backend=args.backend, store=args.store, lock_ms=args.lock_ms,
            prompt_version_served=PROMPT_VERSION_SERVED)
        if args.once:
            outcomes = worker.run_once()
            log("worker_stop", outcomes=outcomes, metrics=worker.metrics)
            return 0
        worker.run(poll_ms=args.poll_ms)
    except KeyboardInterrupt:
        log("worker_stop", metrics=worker.metrics)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
