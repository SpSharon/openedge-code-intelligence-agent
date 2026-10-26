"""Question events — the message contract, and what counts as malformed.

An event is a JSON object:

    {"event_id": "evt-001", "question": "what calls ar-invoice.p?",
     "correlation_id": "req-77",          # optional; defaults to event_id
     "attributes": {"source": "chat"}}    # optional; passed through, not read

A malformed event (not an object, missing or empty event_id/question, wrong
types) raises MalformedEvent. The worker treats that as a poison message:
dead-lettered immediately with the reason, never retried — retrying cannot
fix a message that will never parse, and a retry loop on it would stop every
event behind it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class MalformedEvent(ValueError):
    """The event cannot be parsed; retrying will not help."""


@dataclass(frozen=True)
class QuestionEvent:
    event_id: str
    question: str
    correlation_id: str
    attributes: dict = field(default_factory=dict)


def _non_empty_str(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MalformedEvent(f"{key!r} must be a non-empty string")
    return value.strip()


def parse_event(raw: Any) -> QuestionEvent:
    """Parse a raw body (dict, or a JSON string) into a QuestionEvent."""
    obj = raw
    if isinstance(raw, (str, bytes)):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise MalformedEvent(f"body is not JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise MalformedEvent("body must be a JSON object")
    event_id = _non_empty_str(obj, "event_id")
    question = _non_empty_str(obj, "question")
    correlation_id = obj.get("correlation_id")
    if correlation_id is None:
        correlation_id = event_id
    elif not isinstance(correlation_id, str) or not correlation_id.strip():
        raise MalformedEvent("'correlation_id' must be a non-empty string when present")
    attributes = obj.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise MalformedEvent("'attributes' must be an object when present")
    return QuestionEvent(event_id=event_id, question=question,
                         correlation_id=correlation_id.strip(),
                         attributes=dict(attributes))
