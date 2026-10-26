"""Re-derive the archived scoreboards' stored metrics, offline.

Two checks, both free of API keys and network, both exit non-zero on the
first number that does not reproduce:

1. Every archived answer scoreboard (`evals/results/answers_scoreboard_*.json`)
   is re-scored from its own archived answers with `answer_score.rescore`,
   and every stored aggregate metric (`metrics` and `by_category`) must come
   out identical. This proves the stored Stage 2 / Stage 3 / Bedrock metrics
   follow from the archived answers under the scorer at HEAD. It does not
   regenerate the answers (that needs a key), and it does not compare the
   stored per-case sub-scores, token counts or costs — only what `rescore`
   compares.

2. The Stage 1 headline scoreboard (`evals/results/scoreboard_hybrid.json`)
   is recomputed from the corpus with the shipped defaults, and everything in
   it except the timestamp must be identical — metrics, per-category figures,
   parameters, backend and every per-case rank. The index must exist
   (`python -m openedge_agent.ingest`), and it must be the shipped LSA index:
   with another embedding backend the check is skipped and says so.

Neither check writes anything. Run from the repository root:

    python evals/verify_archive.py            # both checks
    python evals/verify_archive.py --answers  # archived answer scoreboards only
    python evals/verify_archive.py --stage1   # the Stage 1 scoreboard only

`.github/workflows/verify.yml` runs this on every push and pull request, after
the unit tests. A failing run means a stored metric no longer follows from the
archived evidence — report it, do not paper over it. Figures that live only in
prose (the README's tables, costs, token counts) are outside this check.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openedge_agent.score import run as stage1_run  # noqa: E402

# evals/ is not a package: load the scorer by file, the way the tests do.
_spec = importlib.util.spec_from_file_location("answer_score", ROOT / "evals" / "answer_score.py")
answer_score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(answer_score)
rescore = answer_score.rescore

RESULTS = ROOT / "evals" / "results"
STAGE1_ARCHIVE = RESULTS / "scoreboard_hybrid.json"
EMBED_META = ROOT / "index" / "embed_meta.json"
TIMESTAMP_KEY = "run_at"
SKIPPED = -1  # verify_stage1(): the index is not the shipped backend; nothing checked


def answer_boards() -> list[Path]:
    return sorted(RESULTS.glob("answers_scoreboard_*.json"))


def verify_answers() -> tuple[int, int]:
    """Re-score every archived answer scoreboard. Returns (checked, failed)."""
    boards = answer_boards()
    if not boards:
        print("verify_archive: no archived answer scoreboards found")
        return 0, 1
    failed = []
    for board in boards:
        if rescore(board) != 0:
            failed.append(board.name)
    print()
    print(f"verify_archive: {len(boards) - len(failed)} of {len(boards)} archived "
          "answer scoreboards reproduce their stored metrics exactly"
          + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return len(boards), len(failed)


def stage1_differences(fresh: dict, archived: dict) -> list[str]:
    """Every top-level key except the timestamp must match; per-case rows are
    compared one by one so a difference names the case."""
    diffs = []
    for key in sorted(set(fresh) | set(archived)):
        if key == TIMESTAMP_KEY:
            continue
        if key == "cases":
            fresh_cases = {c["id"]: c for c in fresh.get("cases", [])}
            arch_cases = {c["id"]: c for c in archived.get("cases", [])}
            for cid in sorted(set(fresh_cases) | set(arch_cases)):
                if fresh_cases.get(cid) != arch_cases.get(cid):
                    diffs.append(f"cases[{cid}]")
            continue
        if fresh.get(key) != archived.get(key):
            diffs.append(key)
    return diffs


def index_backend() -> str | None:
    """The embedding backend the local index was built with, or None."""
    try:
        return json.loads(EMBED_META.read_text()).get("backend")
    except (OSError, ValueError):
        return None


def verify_stage1() -> int:
    """0 if the Stage 1 headline scoreboard reproduces, 1 if it differs,
    SKIPPED if the local index is not the shipped backend (nothing checked)."""
    if not (ROOT / "index" / "chunks.json").exists():
        print("verify_archive: index/ is missing — run `python -m openedge_agent.ingest` first")
        return 1
    archived = json.loads(STAGE1_ARCHIVE.read_text())
    backend = index_backend()
    if backend != archived.get("embed_backend"):
        print(f"verify_archive: Stage 1 check SKIPPED — the local index was built with "
              f"{backend!r}, the archive with {archived.get('embed_backend')!r}; "
              "rebuild the index with the shipped defaults to run it")
        return SKIPPED
    fresh = stage1_run("hybrid", ROOT, write=False)   # returns the report; writes nothing
    diffs = stage1_differences(fresh, archived)
    m = fresh["metrics"]
    print(f"verify_archive: Stage 1 hybrid recomputed — R@5 {m['micro_recall@5']} · "
          f"R@10 {m['micro_recall@10']} · strict {m['strict_case_recall@5']} · "
          f"P@5 {m['precision@5']} · MRR {m['mrr']} (backend {fresh['embed_backend']})")
    if diffs:
        print(f"verify_archive: Stage 1 scoreboard DIFFERS from the archive in: {', '.join(diffs)}")
        return 1
    print("verify_archive: Stage 1 scoreboard reproduces the archive exactly "
          "(everything except the timestamp)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--answers", action="store_true",
                    help="only the archived answer scoreboards")
    ap.add_argument("--stage1", action="store_true",
                    help="only the Stage 1 retrieval scoreboard")
    args = ap.parse_args()
    do_answers = args.answers or not args.stage1
    do_stage1 = args.stage1 or not args.answers
    failures = 0
    ran = []
    if do_answers:
        checked, failed = verify_answers()
        failures += failed
        ran.append(f"{checked} archived answer scoreboards")
    if do_stage1:
        rc = verify_stage1()
        if rc == SKIPPED:
            ran.append("Stage 1 scoreboard skipped (index not the shipped backend)")
        else:
            failures += rc
            ran.append("the Stage 1 scoreboard")
    if failures:
        print(f"verify_archive: FAILED ({failures} check(s) did not reproduce)")
        return 1
    print("verify_archive: OK — re-derived: " + "; ".join(ran))
    return 0


if __name__ == "__main__":
    sys.exit(main())
