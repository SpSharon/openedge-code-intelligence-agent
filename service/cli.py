"""Command-line client for the service.

    python -m service.cli health
    python -m service.cli ask "what calls ar-invoice.p?" [--k 10] [--max-steps 6] [--json]
    python -m service.cli --url http://127.0.0.1:8000 ...

The access key is sent as X-API-Key: from --key, else from OE_SERVICE_KEY.
Prefer the environment variable - a --key value is kept in shell history.

A non-200 response prints `error <status>: <text>` to stderr and exits 1.
`run(argv, client=...)` accepts any httpx.Client - tests pass FastAPI's
TestClient, so the same code path runs in-process without a server.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def resolve_key(flag: str | None) -> str | None:
    """--key wins; otherwise OE_SERVICE_KEY; otherwise no key."""
    return flag or os.environ.get("OE_SERVICE_KEY") or None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m service.cli",
                                 description="Client for the OpenEdge agent service")
    ap.add_argument("--url", default="http://127.0.0.1:8000",
                    help="service base URL (default %(default)s)")
    ap.add_argument("--key", default=None,
                    help="access key (default: the OE_SERVICE_KEY environment "
                         "variable)")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("health", help="GET /health")
    p_ask = sub.add_parser("ask", help="POST /ask")
    p_ask.add_argument("question")
    p_ask.add_argument("--k", type=int, default=10)
    p_ask.add_argument("--max-steps", type=int, default=6)
    p_ask.add_argument("--json", action="store_true",
                       help="print the raw JSON response")
    return ap


def run(argv: list[str], client: httpx.Client | None = None) -> int:
    args = build_parser().parse_args(argv)
    key = resolve_key(args.key)
    # Per request, not on the client: an injected client gets the header too.
    headers = {"X-API-Key": key} if key else {}
    own_client = client is None
    if own_client:
        client = httpx.Client(base_url=args.url, timeout=120)
    try:
        if args.command == "health":
            r = client.get("/health")
        else:
            r = client.post("/ask", headers=headers,
                            json={"question": args.question,
                                  "k": args.k,
                                  "max_steps": args.max_steps})
    except httpx.TransportError as exc:  # server down, DNS, timeout
        print(f"error: cannot reach {args.url}: {exc}", file=sys.stderr)
        return 1
    finally:
        if own_client:
            client.close()

    if r.status_code != 200:
        print(f"error {r.status_code}: {r.text}", file=sys.stderr)
        return 1

    body = r.json()
    if args.command == "health" or args.json:
        print(json.dumps(body, indent=2))
        return 0

    print(body["answer"] or "(refused: not in the retrieved code)")
    print()
    print("citations:", "; ".join(body["citations"]) or "(none)")
    if body["invalid_citations"]:
        print("INVALID  :", "; ".join(body["invalid_citations"]))
    usage = body["usage"]
    print(f"steps    : {body['steps']}  (model {usage.get('model')}, "
          f"{usage.get('calls')} calls, est. ${usage.get('estimated_cost_usd')})")
    return 0


if __name__ == "__main__":
    # Windows consoles / redirected output may use a legacy code page; never
    # let a non-ASCII answer crash the client with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    sys.exit(run(sys.argv[1:]))
