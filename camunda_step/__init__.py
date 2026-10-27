"""The governed step: the agent as an external task in a Camunda 7 workflow.

`governed-answer.bpmn` — the process (draft → review → approve or reject, a
timer that escalates); `worker.py` — the external-task worker that calls the
Stage 3 agent and writes the audit record back; `run.py` — deploy, start
instances, read the history; `engine.py` — the REST client (standard library).
A local engine (`docker-compose.yml`), a synthetic corpus, nothing deployed.
See README.md, "The governed step in Camunda 7".

Import the modules directly (`from camunda_step.worker import DraftWorker`);
this package file deliberately imports nothing, so `python -m camunda_step.worker`
runs cleanly.
"""
