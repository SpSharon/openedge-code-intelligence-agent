"""The answer store — SQLite, idempotent on event_id.

    answers       one row per event_id; INSERT OR IGNORE, so a redelivered
                  event (same id) writes once and the second write reports
                  inserted=False
    dead_letters  the poison messages and the exhausted retries, with the
                  reason and the attempt count, for a human to look at

The worker writes here BEFORE it acks the source. If the process dies between
the write and the ack, the source redelivers the event, the store says it has
seen the id, and the worker acks without a second model call. That ordering —
commit after the write, idempotent write — is the whole at-least-once story.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from openedge_agent.agent3 import _is_refusal

from .events import QuestionEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS answers (
    event_id        TEXT PRIMARY KEY,
    correlation_id  TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    citations       TEXT NOT NULL,      -- JSON array of unit ids
    refused         INTEGER NOT NULL,   -- 1 when the agent refused (the refusal contract, or no answer)
    steps           INTEGER,            -- tool steps, when the agent reports them
    attempt         INTEGER NOT NULL,   -- the delivery attempt that succeeded
    duration_ms     INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_letters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT,               -- NULL when the body never parsed
    receipt         TEXT NOT NULL,
    reason          TEXT NOT NULL,
    body            TEXT NOT NULL,      -- the raw body, as JSON text
    attempts        INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AnswerStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- idempotency --

    def has(self, event_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM answers WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None

    def insert_answer(self, event: QuestionEvent, result: dict, *,
                      attempt: int, duration_ms: int) -> bool:
        """Write the answer once. Returns False if event_id was already there."""
        answer = result.get("answer", "") or ""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO answers (event_id, correlation_id, question, "
            "answer, citations, refused, steps, attempt, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.event_id, event.correlation_id, event.question, answer,
             json.dumps(list(result.get("citations", []) or [])),
             1 if (answer == "" or _is_refusal(answer)) else 0, result.get("steps"),
             attempt, duration_ms, _now_iso()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    # -- dead letters --

    @staticmethod
    def _body_text(body) -> str:
        try:
            return json.dumps(body)
        except (TypeError, ValueError):
            return json.dumps(str(body))

    def has_dead_letter(self, *, event_id: str | None = None, body=None) -> bool:
        """Already dead-lettered? By event_id when the body parsed; by the raw
        body otherwise (a poison message has no id). Makes the dead-letter
        path idempotent on redelivery, like the answer path."""
        if event_id is not None:
            row = self.conn.execute(
                "SELECT 1 FROM dead_letters WHERE event_id = ?", (event_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM dead_letters WHERE event_id IS NULL AND body = ?",
                (self._body_text(body),)).fetchone()
        return row is not None

    def record_dead_letter(self, *, event_id: str | None, receipt: str,
                           reason: str, body, attempts: int) -> None:
        body_text = self._body_text(body)
        self.conn.execute(
            "INSERT INTO dead_letters (event_id, receipt, reason, body, attempts, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, receipt, reason, body_text, attempts, _now_iso()),
        )
        self.conn.commit()

    # -- reading back --

    def answer(self, event_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM answers WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["citations"] = json.loads(out["citations"])
        return out

    def answers(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM answers ORDER BY created_at, rowid").fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["citations"] = json.loads(d["citations"])
            out.append(d)
        return out

    def dead_letters(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM dead_letters ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict:
        n_answers = self.conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        n_dead = self.conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
        return {"answers": n_answers, "dead_letters": n_dead}
