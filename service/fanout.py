"""Concurrent fan-out of an eval cases file against the running service.

    python -m service.fanout --url http://127.0.0.1:8000 \
        --cases evals/answers_heldout.jsonl --concurrency 3

asyncio.gather runs one coroutine per case; an asyncio.Semaphore caps how
many requests are in flight. gather returns results in input order, so the
output follows the cases file regardless of completion order.
return_exceptions=True keeps every result: a failed request becomes an
{"id", "error"} item instead of discarding the others. Exit code 1 if any
case failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx


def load_cases(path: Path) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


async def ask_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                  case: dict) -> dict:
    async with sem:
        t0 = time.perf_counter()
        r = await client.post("/ask", json={"question": case["question"]})
        r.raise_for_status()
        body = r.json()
        return {
            "id": case["id"],
            "steps": body["steps"],
            "n_citations": len(body["citations"]),
            "refused": body["answer"] == "",
            "seconds": round(time.perf_counter() - t0, 3),
        }


async def run_all(client: httpx.AsyncClient, cases: list[dict],
                  concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(*(ask_one(client, sem, c) for c in cases),
                                   return_exceptions=True)
    out = []
    for case, res in zip(cases, results):
        if isinstance(res, BaseException):
            out.append({"id": case["id"], "error": repr(res)})
        else:
            out.append(res)
    return out


async def main_async(url: str, cases_path: Path, concurrency: int) -> int:
    cases = load_cases(cases_path)
    t0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=url, timeout=300) as client:
        results = await run_all(client, cases, concurrency)
    elapsed = time.perf_counter() - t0
    for res in results:
        print(json.dumps(res))
    per_case = sum(r.get("seconds", 0.0) for r in results)
    print(f"{len(results)} cases in {elapsed:.2f}s at concurrency {concurrency} "
          f"(sum of per-case times {per_case:.2f}s)")
    return 1 if any("error" in r for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m service.fanout",
                                 description="Concurrent fan-out over a cases file")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--cases", type=Path, default=Path("evals/answers_heldout.jsonl"))
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args(argv)
    if args.concurrency < 1:
        ap.error("--concurrency must be at least 1")
    return asyncio.run(main_async(args.url, args.cases, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
