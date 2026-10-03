"""FastAPI service over the Stage 3 tool agent.

    bash:        OE_AGENT_BACKEND=fake python -m uvicorn service.app:app --port 8000
    PowerShell:  $env:OE_AGENT_BACKEND = "fake"; python -m uvicorn service.app:app --port 8000

Configuration is read once, in `lifespan`: OE_AGENT_BACKEND (default
"anthropic", as in openedge_agent.llm.make_llm) and OE_INDEX_DIR (default
"index"). Keys (ANTHROPIC_API_KEY, AWS credentials) are read only by the
agent's own backends, from the environment.

One ToolAgent per request: ToolAgent.ask() resets and fills per-question
state (read_set, seen_ids) that decides which citations are valid, so an
instance shared across the thread pool would race.

Status codes:
    422  bad request body (Pydantic, before any agent is built); the
         question is stripped of surrounding whitespace before its
         3-character minimum is checked
    503  configuration: unknown backend, missing ANTHROPIC_API_KEY, missing
         OE_BEDROCK_MODEL_ID, no AWS credentials for Bedrock, backend SDK
         not installed - detected while building the agent, before the
         endpoint body runs, so /ask/stream also refuses with a 503 before
         it starts
    500  anything else, including any error raised inside agent.ask() -
         bugs are not caught and relabelled.
         Known limit: errors from the live model API itself - an invalid or
         revoked Anthropic key, upstream rate limits / overload, network
         failures, Bedrock access-denied - are raised inside agent.ask() and
         also return 500; they are not mapped to 502/503/429.

/ask/stream caveat: once the first line has been sent the status (200) and
headers are already on the wire. A failure after that point (e.g. inside
agent.ask()) cannot become a 500: the server logs the exception and closes
the stream, and the client sees 200 with a truncated body - a "retrieved"
line and no "answer" line. Clients must treat a missing "answer" event as
a failure.

/health always answers 200 while the process is up (it is a liveness and
diagnostic check). It reports status "ok", or "misconfigured" with the
reason in `problem` when OE_AGENT_BACKEND is not one of the backends
openedge_agent.llm knows; /ask then answers 503 with the same reason.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException

from openedge_agent.agent3 import ToolAgent
from openedge_agent.llm import BACKENDS, BedrockLLM, make_llm
from openedge_agent.retrieve import Retriever

from .models import AskRequest, AskResponse, HealthResponse, StreamEvent

# Filled once by lifespan; read by every request.
settings: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    index_dir = Path(os.environ.get("OE_INDEX_DIR", "index"))
    if not (index_dir / "chunks.json").exists():
        raise RuntimeError(
            f"no index at {index_dir}; run: python -m openedge_agent.ingest"
        )
    settings["index_dir"] = index_dir
    settings["backend"] = (
        os.environ.get("OE_AGENT_BACKEND") or "anthropic"
    ).strip().lower()
    settings["problem"] = (
        None if settings["backend"] in BACKENDS
        else f"unknown backend {settings['backend']!r}; expected one of {BACKENDS}"
    )
    # Retriever.units includes the schema units: 35 on this corpus.
    settings["units"] = len(Retriever(index_dir=index_dir).units)
    yield
    settings.clear()


app = FastAPI(title="OpenEdge code-intelligence service", lifespan=lifespan)


def _check_aws_credentials() -> None:
    """boto3 builds a Bedrock client without checking credentials; a missing
    credential would only surface as NoCredentialsError inside agent.ask()
    (a 500). Resolve the credential chain now instead."""
    import boto3  # lazy: only on the Bedrock path; installed if we got here

    if boto3.Session().get_credentials() is None:
        raise HTTPException(
            status_code=503,
            detail="no AWS credentials found for the Bedrock backend (set "
                   "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE or an "
                   "attached role in the environment - never in a file)",
        )


def _preflight(llm) -> None:
    """Surface the backend's configuration errors now, not mid-request.

    AnthropicLLM and BedrockLLM check their configuration lazily, in
    _ensure_client(), on the first complete() call. Calling it here (it
    makes no model call) turns a missing key / model id into a 503 before
    the agent runs; FakeLLM has no such method. For Bedrock the AWS
    credential chain is resolved too."""
    ensure = getattr(llm, "_ensure_client", None)
    if ensure is None:
        return
    try:
        ensure()
    except RuntimeError as exc:  # missing ANTHROPIC_API_KEY / OE_BEDROCK_MODEL_ID / boto3
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ImportError as exc:  # anthropic SDK not installed
        raise HTTPException(
            status_code=503, detail=f"backend SDK not installed: {exc}"
        ) from exc
    if isinstance(llm, BedrockLLM):
        _check_aws_credentials()


def new_agent(req: AskRequest) -> ToolAgent:
    """A fresh, configured agent for this request (or a 503)."""
    try:
        llm = make_llm(settings["backend"])
    except ValueError as exc:  # unknown backend name
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _preflight(llm)
    return ToolAgent(
        index_dir=settings["index_dir"], llm=llm, k=req.k, max_steps=req.max_steps
    )


def ask_job(req: AskRequest) -> tuple[AskRequest, ToolAgent]:
    """Dependency for both /ask routes. It runs before the endpoint, so a
    configuration error is a 503 even for the stream (whose generator body
    only runs after the 200 and its headers have been sent). The body is
    declared here only, so a bad body is validated - and reported - once."""
    return req, new_agent(req)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    problem = settings["problem"]
    return HealthResponse(
        status="misconfigured" if problem else "ok",
        backend=settings["backend"],
        index_dir=str(settings["index_dir"]),
        units=settings["units"],
        problem=problem,
    )


@app.post("/ask", response_model=AskResponse)
def ask(job: tuple[AskRequest, ToolAgent] = Depends(ask_job)) -> AskResponse:
    req, agent = job
    # Plain def: agent.ask() blocks on the model, so FastAPI runs this in
    # its thread pool and the event loop stays free.
    out = agent.ask(req.question)
    return AskResponse(
        answer=out["answer"],
        citations=out["citations"],
        invalid_citations=out["invalid_citations"],
        retrieved=out["retrieved"],
        steps=out["steps"],
        backend=settings["backend"],
        usage=agent.llm.meter.as_dict(),
    )


@app.post("/ask/stream")
def ask_stream(
    job: tuple[AskRequest, ToolAgent] = Depends(ask_job),
) -> Iterable[StreamEvent]:
    """JSON Lines (application/jsonl): the retrieval seed as soon as it
    exists, then the answer when the tool loop finishes. ask() repeats the
    same deterministic seed search internally."""
    req, agent = job
    hits = agent.retriever.search(req.question, k=agent.k)
    yield StreamEvent(event="retrieved", data={"ids": [h["id"] for h in hits]})
    out = agent.ask(req.question)
    yield StreamEvent(
        event="answer",
        data={
            "answer": out["answer"],
            "citations": out["citations"],
            "invalid_citations": out["invalid_citations"],
            "steps": out["steps"],
        },
    )
