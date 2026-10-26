"""Event sources — the protocol, an in-memory queue with queue semantics, a file.

EventSource is the seam where a real broker client goes (SQS, Kafka, ...).
The worker only ever calls these four methods:

    fetch(max_events)        -> list[Envelope]   hidden from other consumers
                                                 until acked or the visibility
                                                 timeout passes
    ack(envelope)                               the work is durable; delete it
    nack(envelope, delay)                       make it visible again after
                                                `delay` seconds (a retry)
    dead_letter(envelope, reason)               take it out of the stream for
                                                a human; keep the reason

MemorySource implements those with the semantics the worker is written
against — the same ones SQS gives you:

  * at-least-once: a fetched message that is not acked before its visibility
    timeout is delivered again, attempt + 1 (this is why the store is
    idempotent on event_id and why the worker writes before it acks);
  * a receive-count redrive: a message delivered more than max_receive_count
    times goes to the dead letters by the source itself, even if the worker
    never gets as far as a decision (the SQS redrive policy);
  * order: messages are offered in arrival order; a retried message comes
    back when its delay has passed, behind whatever was visible by then
    (fetch orders by the time a message became visible, then by arrival);
  * a receive-count redrive is reported through on_redrive(body, attempts,
    reason) when a callback is set, so a worker can record it.

The clock is injectable so the tests move time instead of sleeping.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol


@dataclass
class Envelope:
    receipt: str              # handle for ack / nack / dead_letter
    body: Any                 # raw body as received (dict or str)
    attempt: int              # 1 on first delivery
    received_at: float
    meta: dict = field(default_factory=dict)


class EventSource(Protocol):
    def fetch(self, max_events: int = 10) -> list[Envelope]: ...
    def ack(self, envelope: Envelope) -> None: ...
    def nack(self, envelope: Envelope, delay_seconds: float = 0.0) -> None: ...
    def dead_letter(self, envelope: Envelope, reason: str) -> None: ...


@dataclass
class _Message:
    seq: int
    body: Any
    receipts: int = 0          # how many times delivered
    visible_at: float = 0.0    # hidden until this clock time
    in_flight_receipt: str | None = None


class MemorySource:
    """An in-memory queue with visibility timeout, redelivery and redrive."""

    def __init__(self, visibility_timeout: float = 30.0,
                 max_receive_count: int | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_redrive: Callable[[Any, int, str], None] | None = None):
        if visibility_timeout <= 0:
            raise ValueError("visibility_timeout must be > 0")
        self.visibility_timeout = visibility_timeout
        self.max_receive_count = max_receive_count
        self.clock = clock
        self.on_redrive = on_redrive
        self._seq = itertools.count(1)
        self._messages: dict[int, _Message] = {}
        self._receipts: dict[str, int] = {}          # receipt -> seq
        self.dead_letters: list[dict] = []
        self.acked: list[str] = []

    # -- producer side --

    def put(self, body: Any) -> int:
        seq = next(self._seq)
        self._messages[seq] = _Message(seq=seq, body=body,
                                       visible_at=self.clock())
        return seq

    def put_many(self, bodies: Iterable[Any]) -> int:
        n = 0
        for body in bodies:
            self.put(body)
            n += 1
        return n

    # -- consumer side --

    def fetch(self, max_events: int = 10) -> list[Envelope]:
        now = self.clock()
        out: list[Envelope] = []
        # order: by the time a message became (or becomes) visible, then by
        # arrival — fresh puts keep arrival order; a retried message lands
        # behind what was visible when its delay elapsed
        for msg in sorted(self._messages.values(),
                          key=lambda m: (m.visible_at, m.seq)):
            if len(out) >= max_events:
                break
            if msg.visible_at > now:
                continue                      # hidden (in flight) or delayed
            if msg.in_flight_receipt is not None:
                # visibility timeout passed without an ack: redeliver
                self._receipts.pop(msg.in_flight_receipt, None)
                msg.in_flight_receipt = None
            if (self.max_receive_count is not None
                    and msg.receipts >= self.max_receive_count):
                # delivered max_receive_count times already, never acked
                self._to_dead_letters(msg, reason="max_receive_count exceeded")
                continue
            msg.receipts += 1
            receipt = f"r{msg.seq}-{msg.receipts}"
            msg.in_flight_receipt = receipt
            msg.visible_at = now + self.visibility_timeout
            self._receipts[receipt] = msg.seq
            out.append(Envelope(receipt=receipt, body=msg.body,
                                attempt=msg.receipts, received_at=now,
                                meta={"seq": msg.seq}))
        return out

    def ack(self, envelope: Envelope) -> None:
        seq = self._receipts.pop(envelope.receipt, None)
        if seq is None:
            raise KeyError(f"unknown or expired receipt {envelope.receipt!r}")
        self._messages.pop(seq, None)
        self.acked.append(envelope.receipt)

    def nack(self, envelope: Envelope, delay_seconds: float = 0.0) -> None:
        seq = self._receipts.pop(envelope.receipt, None)
        if seq is None:
            raise KeyError(f"unknown or expired receipt {envelope.receipt!r}")
        msg = self._messages[seq]
        msg.in_flight_receipt = None
        msg.visible_at = self.clock() + max(0.0, delay_seconds)

    def dead_letter(self, envelope: Envelope, reason: str) -> None:
        seq = self._receipts.pop(envelope.receipt, None)
        if seq is None:
            raise KeyError(f"unknown or expired receipt {envelope.receipt!r}")
        self._to_dead_letters(self._messages[seq], reason=reason)

    def _to_dead_letters(self, msg: _Message, reason: str) -> None:
        self._messages.pop(msg.seq, None)
        self.dead_letters.append({"seq": msg.seq, "body": msg.body,
                                  "attempts": msg.receipts, "reason": reason})
        if reason.startswith("max_receive_count") and self.on_redrive is not None:
            self.on_redrive(msg.body, msg.receipts, reason)

    # -- observability --

    def pending(self) -> int:
        """Messages not yet acked or dead-lettered (visible or not)."""
        return len(self._messages)

    def next_visible_in(self) -> float | None:
        """Seconds until the earliest hidden/delayed message becomes visible;
        0.0 if something is visible now; None if the queue is empty."""
        if not self._messages:
            return None
        now = self.clock()
        return max(0.0, min(m.visible_at for m in self._messages.values()) - now)


class FileSource(MemorySource):
    """A JSON Lines file of events, loaded into a MemorySource.

    One event per line. Lines that are not JSON are kept as raw strings so
    the worker dead-letters them with a reason instead of the loader
    crashing — a poison line is data about the pipeline, not a load error.
    """

    def __init__(self, path: str | Path, **kwargs):
        super().__init__(**kwargs)
        self.path = Path(path)
        # utf-8-sig: a UTF-8 byte-order mark (Notepad "UTF-8 with BOM",
        # PowerShell 5.1 `-Encoding utf8`) is dropped instead of making the
        # first event malformed. UTF-16 — PowerShell 5.1's `Out-File` and `>`
        # default — is not UTF-8 and fails the decode; run.py says so and
        # exits 2
        for line in self.path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            try:
                self.put(json.loads(line))
            except ValueError:
                self.put(line)
