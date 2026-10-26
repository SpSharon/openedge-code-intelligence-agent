"""The worker — fetch, parse, check, ask, write, ack; retry or dead-letter.

Per envelope, in this order:

    1. parse            MalformedEvent -> dead-letter now (poison message)
    2. idempotency      store.has(event_id) -> ack without asking again
                        (a redelivery after a crash between write and ack)
    3. ask              agent.ask(question); a failure is transient unless it
                        is permanent (is_permanent: a missing package, an
                        authentication or not-found error from the model API,
                        a configuration that can never succeed) — those are
                        re-raised, because retrying them 3x per event and
                        dead-lettering the whole input would be wrong
    4. write            store.insert_answer(...)  -- the durable step
    5. ack              source.ack(envelope)      -- only after the write

On a transient failure the envelope is nacked with a delay from the backoff
schedule (exponential, capped, with jitter) until max_attempts, then
dead-lettered with the last error as the reason. A failure of the source call
itself (a stale receipt, say) is logged and the loop continues — the store is
idempotent on both paths (answers by event_id; dead letters by event_id, or by
body for a poison message), so a redelivery after a failed ack or dead_letter
call is absorbed without a second model call or a second row.

Every outcome is one JSON log line on the "consumer" logger: event, event_id,
correlation_id, receipt, attempt; duration_ms when the model was called;
reason when there is one. The same reason string goes to the log, the store
and the source.

The worker never sleeps inside run_once(); run() sleeps only when nothing is
visible, for as long as the source says the next message needs — through an
injectable sleep, so tests move a fake clock instead.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Callable

from .events import MalformedEvent, QuestionEvent, parse_event
from .source import Envelope, EventSource
from .store import AnswerStore

log = logging.getLogger("consumer")

# Failures that no retry can fix. Anything else raised by agent.ask() is
# treated as transient (a model API hiccup, a timeout, a 429).
PERMANENT_ERRORS: tuple[type[BaseException], ...] = (ImportError, NotImplementedError)
# HTTP statuses the model SDKs put on their exceptions (anthropic: .status_code)
# that a retry cannot change: bad key, forbidden, unknown model.
PERMANENT_STATUS_CODES = frozenset({401, 403, 404})
# botocore ClientError codes (exc.response["Error"]["Code"]) of the same kind,
# plus the credential exceptions by class name — no SDK import needed.
PERMANENT_AWS_CODES = frozenset({
    "UnrecognizedClientException", "InvalidSignatureException",
    "AccessDeniedException", "ExpiredTokenException",
    "ResourceNotFoundException", "ValidationException",
})
PERMANENT_EXCEPTION_NAMES = frozenset({"NoCredentialsError", "PartialCredentialsError"})


def is_permanent(exc: BaseException,
                 permanent_errors: tuple[type[BaseException], ...] = PERMANENT_ERRORS) -> bool:
    """True when retrying cannot help: wrong type of failure, not a slow one."""
    if isinstance(exc, permanent_errors):
        return True
    if getattr(exc, "status_code", None) in PERMANENT_STATUS_CODES:
        return True
    if type(exc).__name__ in PERMANENT_EXCEPTION_NAMES:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code in PERMANENT_AWS_CODES:
            return True
    return False


@dataclass
class Backoff:
    """delay(attempt) = min(cap, base * 2**(attempt-1)) + jitter.

    attempt is the delivery attempt that just failed (1 on the first try).
    jitter is a fraction of the delay, drawn from rand() in [0, 1).
    """
    base: float = 0.5
    cap: float = 8.0
    jitter: float = 0.25
    rand: Callable[[], float] = random.random

    def delay(self, attempt: int) -> float:
        exp = min(self.cap, self.base * (2 ** max(0, attempt - 1)))
        return exp + exp * self.jitter * self.rand()


@dataclass
class Metrics:
    fetched: int = 0
    processed: int = 0
    duplicates: int = 0
    retried: int = 0
    dead_lettered: int = 0      # by the worker: poison messages + exhausted retries
    malformed: int = 0
    redriven: int = 0           # by the source: receive count exceeded
    source_errors: int = 0      # ack/nack/dead_letter calls that failed
    duration_ms_total: int = 0
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Worker:
    def __init__(self, agent, source: EventSource, store: AnswerStore, *,
                 max_attempts: int = 3, backoff: Backoff | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 batch_size: int = 10,
                 permanent_errors: tuple[type[BaseException], ...] = PERMANENT_ERRORS):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.agent = agent
        self.source = source
        self.store = store
        self.max_attempts = max_attempts
        self.backoff = backoff or Backoff()
        self.clock = clock
        self.sleep = sleep
        self.batch_size = batch_size
        self.permanent_errors = permanent_errors
        self.metrics = Metrics()
        # a source that redrives by receive count tells us, so the dead letter
        # is in the store and in the metrics like every other one
        if getattr(source, "on_redrive", "absent") is None:
            source.on_redrive = self._on_redrive

    # -- logging --

    def _log(self, event: str, *, envelope: Envelope | None = None,
             question_event: QuestionEvent | None = None,
             duration_ms: int | None = None, reason: str | None = None,
             level: int = logging.INFO) -> None:
        payload = {
            "event": event,
            "event_id": question_event.event_id if question_event else None,
            "correlation_id": question_event.correlation_id if question_event else None,
            "receipt": envelope.receipt if envelope else None,
            "attempt": envelope.attempt if envelope else None,
        }
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if reason is not None:
            payload["reason"] = reason
        log.log(level, json.dumps(payload, sort_keys=True))

    # -- source calls that must not kill the loop --

    def _source_call(self, name: str, *args) -> bool:
        try:
            getattr(self.source, name)(*args)
            return True
        except Exception as exc:  # noqa: BLE001 — a broker error; the loop goes on
            self.metrics.source_errors += 1
            envelope = args[0] if args else None
            self._log("source_error", envelope=envelope,
                      reason=f"{name} failed: {type(exc).__name__}: {exc}",
                      level=logging.ERROR)
            return False

    def _on_redrive(self, body, attempts: int, reason: str) -> None:
        event_id = None
        try:
            event_id = parse_event(body).event_id
        except MalformedEvent:
            pass
        self.store.record_dead_letter(event_id=event_id, receipt="redrive",
                                      reason=reason, body=body, attempts=attempts)
        self.metrics.redriven += 1
        self.metrics.reasons.append(reason)
        log.log(logging.ERROR, json.dumps({
            "event": "dead_letter", "event_id": event_id, "correlation_id": None,
            "receipt": "redrive", "attempt": attempts, "reason": reason,
        }, sort_keys=True))

    # -- the loop --

    def run_once(self, max_events: int | None = None) -> int:
        """Fetch one batch and process it. Returns the number fetched."""
        batch = self.source.fetch(max_events or self.batch_size)
        self.metrics.fetched += len(batch)
        for envelope in batch:
            self._handle(envelope)
        return len(batch)

    def run(self, max_batches: int | None = None) -> Metrics:
        """Process until the source is empty (or max_batches), sleeping only
        while everything left is hidden or delayed."""
        batches = 0
        while max_batches is None or batches < max_batches:
            n = self.run_once()
            batches += 1
            if n:
                continue
            wait = getattr(self.source, "next_visible_in", lambda: None)()
            if wait is None:
                break                           # nothing left anywhere
            if wait > 0:
                self.sleep(wait)
        return self.metrics

    # -- one envelope --

    def _handle(self, envelope: Envelope) -> None:
        # 1. parse — a malformed body is a poison message
        try:
            event = parse_event(envelope.body)
        except MalformedEvent as exc:
            reason = f"malformed: {exc}"
            if self.store.has_dead_letter(body=envelope.body):
                # redelivered after a failed dead_letter call: no second row
                self._source_call("dead_letter", envelope, reason)
                self.metrics.duplicates += 1
                self._log("duplicate", envelope=envelope,
                          reason="already dead-lettered")
                return
            self.store.record_dead_letter(event_id=None, receipt=envelope.receipt,
                                          reason=reason, body=envelope.body,
                                          attempts=envelope.attempt)
            self._source_call("dead_letter", envelope, reason)
            self.metrics.malformed += 1
            self.metrics.dead_lettered += 1
            self.metrics.reasons.append(reason)
            self._log("dead_letter", envelope=envelope, reason=reason,
                      level=logging.WARNING)
            return

        # 2. idempotency — already written: a redelivery, ack and move on;
        #    already dead-lettered: re-issue the dead letter, no model call
        if self.store.has(event.event_id):
            self._source_call("ack", envelope)
            self.metrics.duplicates += 1
            self._log("duplicate", envelope=envelope, question_event=event,
                      reason="already stored")
            return
        if self.store.has_dead_letter(event_id=event.event_id):
            self._source_call("dead_letter", envelope, "already dead-lettered")
            self.metrics.duplicates += 1
            self._log("duplicate", envelope=envelope, question_event=event,
                      reason="already dead-lettered")
            return

        # 3. ask
        t0 = self.clock()
        try:
            result = self.agent.ask(event.question)
        except Exception as exc:  # noqa: BLE001 — classified below
            if is_permanent(exc, self.permanent_errors):
                raise                           # nothing a retry can fix: stop
            duration_ms = int((self.clock() - t0) * 1000)
            error = f"{type(exc).__name__}: {exc}"
            if envelope.attempt < self.max_attempts:
                delay = self.backoff.delay(envelope.attempt)
                self._source_call("nack", envelope, delay)
                self.metrics.retried += 1
                self._log("retry", envelope=envelope, question_event=event,
                          duration_ms=duration_ms,
                          reason=f"{error}; retry in {delay:.2f}s",
                          level=logging.WARNING)
            else:
                reason = f"exhausted after {envelope.attempt} attempts: {error}"
                self.store.record_dead_letter(event_id=event.event_id,
                                              receipt=envelope.receipt,
                                              reason=reason, body=envelope.body,
                                              attempts=envelope.attempt)
                self._source_call("dead_letter", envelope, reason)
                self.metrics.dead_lettered += 1
                self.metrics.reasons.append(reason)
                self._log("dead_letter", envelope=envelope, question_event=event,
                          duration_ms=duration_ms, reason=reason,
                          level=logging.ERROR)
            return
        duration_ms = int((self.clock() - t0) * 1000)

        # 4. write — the durable step; 5. ack — only after it
        inserted = self.store.insert_answer(event, result, attempt=envelope.attempt,
                                            duration_ms=duration_ms)
        self._source_call("ack", envelope)
        self.metrics.duration_ms_total += duration_ms
        if inserted:
            self.metrics.processed += 1
            self._log("processed", envelope=envelope, question_event=event,
                      duration_ms=duration_ms)
        else:
            # raced with another worker between has() and insert(): still one row
            self.metrics.duplicates += 1
            self._log("duplicate", envelope=envelope, question_event=event,
                      duration_ms=duration_ms, reason="inserted elsewhere")
