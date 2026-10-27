"""Deploy the process, start instances, read what the engine remembers.

    python -m camunda_step.run deploy                      # the BPMN, once (duplicate filtering on)
    python -m camunda_step.run start --instances 3         # three questions from the held-out set
    python -m camunda_step.run start --question "what calls ar-invoice.p?" --timeout PT2M
    python -m camunda_step.run tasks                       # the review tasks waiting in Tasklist
    python -m camunda_step.run history <processInstanceId> # activities, who decided what, the variables

`history` prints the audit trail the bid calls auditability: every activity with
its start and end, the user operations (who completed the review task, when),
and the variables — prompt version, model id, token counts, the decision, the
reviewer's note — as the engine recorded them. The same facts are in Cockpit;
this is the API view of them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .engine import DEFAULT_URL, Engine, EngineError, EngineTransient

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BPMN = HERE / "governed-answer.bpmn"
PROCESS_KEY = "governed-answer"
HELDOUT = ROOT / "evals" / "answers_heldout.jsonl"
DEFAULT_PROMPT_VERSION = "agent3-2026-07-26"
ISO_DURATION = re.compile(r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$")


def heldout_questions(n: int) -> list[str]:
    qs = []
    for line in HELDOUT.read_text(encoding="utf-8").splitlines():
        if line.strip():
            qs.append(json.loads(line)["question"])
    return qs[:n]


def cmd_deploy(engine: Engine, args) -> int:
    dep = engine.deploy("governed-answer", BPMN)
    defs = dep.get("deployedProcessDefinitions") or {}
    versions = [f"{d.get('key')} v{d.get('version')}" for d in defs.values()] or ["no new version (unchanged BPMN)"]
    print(f"deployed {dep.get('id')}: {', '.join(versions)}")
    return 0


def cmd_start(engine: Engine, args) -> int:
    if not ISO_DURATION.match(args.timeout):
        print(f"--timeout must be an ISO-8601 duration such as PT2M or PT10M, not {args.timeout!r} "
              "(the review's boundary timer reads it; a bad value fails the step after the draft)",
              file=sys.stderr)
        return 1
    questions = [args.question] if args.question else heldout_questions(args.instances)
    for i, q in enumerate(questions, 1):
        inst = engine.start(PROCESS_KEY, {"question": q, "prompt_version": args.prompt_version,
                                          "approvalTimeout": args.timeout},
                            business_key=f"demo-{i}")
        print(f"started {inst['id']} (business key {inst.get('businessKey')}): {q[:90]}")
    print(f"{len(questions)} instance(s) started; the worker drafts, then the review tasks appear in Tasklist "
          f"(http://localhost:8080/camunda/app/tasklist/ — demo / demo)")
    return 0


def cmd_tasks(engine: Engine, args) -> int:
    tasks = engine.tasks(key=PROCESS_KEY)
    if not tasks:
        print("no review tasks waiting")
        return 0
    for t in tasks:
        print(f"{t['id']}  instance {t.get('processInstanceId')}  {t.get('name')}  assignee={t.get('assignee')}  created {t.get('created')}")
    return 0


def cmd_history(engine: Engine, args) -> int:
    inst = engine.history_instance(args.instance_id)
    if not inst:
        print("unknown instance")
        return 1
    print(f"instance {inst['id']}  key={inst.get('processDefinitionKey')}  state={inst.get('state')}  "
          f"started {inst.get('startTime')}  ended {inst.get('endTime') or '-'}")
    print("\nactivities, in order:")
    for a in engine.history_activities(args.instance_id):
        who = f"  by {a['assignee']}" if a.get("assignee") else ""
        print(f"  {a.get('activityType'):<22} {a.get('activityName') or a.get('activityId'):<36} "
              f"{a.get('startTime')} -> {a.get('endTime') or 'open'}{who}")
    ops = engine.history_operations(args.instance_id)
    if ops:
        print("\nuser operations (who did what):")
        for o in ops:
            print(f"  {o.get('timestamp')}  {o.get('userId')}  {o.get('operationType')}  "
                  f"{o.get('property') or ''} {o.get('newValue') or ''}")
    print("\nvariables, latest values:")
    seen = {}
    for v in engine.history_variables(args.instance_id):
        seen[v["name"]] = v
    for name in sorted(seen):
        val = seen[name].get("value")
        text = json.dumps(val, ensure_ascii=False)
        print(f"  {name:<24} {text[:160]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engine", default=os.environ.get("CAMUNDA_URL", DEFAULT_URL))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deploy")
    s = sub.add_parser("start")
    s.add_argument("--instances", type=int, default=3)
    s.add_argument("--question", default=None)
    s.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    s.add_argument("--timeout", default="PT10M", help="ISO-8601 duration for the review boundary timer")
    sub.add_parser("tasks")
    h = sub.add_parser("history")
    h.add_argument("instance_id")
    args = ap.parse_args(argv)
    engine = Engine(args.engine)
    try:
        return {"deploy": cmd_deploy, "start": cmd_start, "tasks": cmd_tasks, "history": cmd_history}[args.cmd](engine, args)
    except EngineTransient as exc:
        print(f"engine unreachable at {args.engine}: {exc}", file=sys.stderr)
        return 2
    except EngineError as exc:
        print(f"engine refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
