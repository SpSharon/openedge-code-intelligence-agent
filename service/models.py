"""Request and response shapes for the service (Pydantic v2)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


class AskRequest(BaseModel):
    # stripped first, so "   " (or "  a ") fails the 3-character minimum
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)
    ]
    k: int = Field(default=10, ge=1, le=50)
    max_steps: int = Field(default=6, ge=1, le=12)


class AskResponse(BaseModel):
    """The client-facing subset of ToolAgent.ask(), plus backend and usage.

    raw_reply, read and trace stay internal on purpose."""

    answer: str
    citations: list[str]
    invalid_citations: list[str]
    retrieved: list[str]
    steps: int
    backend: str
    usage: dict


class HealthResponse(BaseModel):
    status: str
    backend: str
    index_dir: str
    units: int
    problem: str | None = None  # set when status is "misconfigured"


class StreamEvent(BaseModel):
    event: str
    data: dict
