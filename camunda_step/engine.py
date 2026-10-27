"""A thin client for the Camunda 7 REST API — only what the governed step needs.

Standard library only (urllib), so the core suite's numpy-only rule holds. Every
call raises `EngineTransient` for what a worker should retry (connection
errors, 5xx, 429, timeouts) and `EngineError` for what it should not (4xx:
a lock that is no longer ours, a bad request, an unknown id).

Endpoints used (Camunda 7 REST, `/engine-rest` on the default distribution):
    POST /external-task/fetchAndLock       fetch_and_lock
    POST /external-task/{id}/complete      complete
    POST /external-task/{id}/failure       failure
    POST /external-task/{id}/extendLock    extend_lock
    POST /deployment/create                deploy
    POST /process-definition/key/{k}/start start
    GET  /process-instance, /task, /history/activity-instance,
         /history/variable-instance, /history/user-operation-log
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://localhost:8080/engine-rest"
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class EngineError(Exception):
    """The engine refused the request (4xx): do not retry blindly."""

    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"{status} from {path}: {body[:300]}")
        self.status = status
        self.body = body
        self.path = path


class EngineTransient(Exception):
    """The engine was unreachable or answered 5xx/429: retry with backoff."""


def typed(value: Any) -> dict[str, Any]:
    """A Python value as a Camunda typed variable {value, type}."""
    if isinstance(value, bool):
        return {"value": value, "type": "Boolean"}
    if isinstance(value, int):
        # Camunda's Integer is 32-bit; anything larger goes as Long
        return {"value": value, "type": "Integer" if -2**31 <= value < 2**31 else "Long"}
    if isinstance(value, float):
        return {"value": value, "type": "Double"}
    if value is None:
        return {"value": None, "type": "Null"}
    return {"value": str(value), "type": "String"}


def plain(variables: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """Camunda typed variables {name: {value, type}} -> {name: value}."""
    return {k: (v or {}).get("value") for k, v in (variables or {}).items()}


class Engine:
    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 30.0, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str = "application/json", query: dict | None = None,
                 timeout: float | None = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", content_type)
        req.add_header("Accept", "application/json")
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if exc.code in TRANSIENT_STATUSES:
                raise EngineTransient(f"{exc.code} from {path}: {text[:200]}") from exc
            raise EngineError(exc.code, text, path) from exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as exc:
            raise EngineTransient(f"{path}: {exc}") from exc
        if status == 204 or not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:   # a non-JSON body from a proxy or a half-up engine
            raise EngineTransient(f"{path}: unreadable response ({exc})") from exc

    def _post_json(self, path: str, payload: dict, timeout: float | None = None) -> Any:
        return self._request("POST", path, json.dumps(payload).encode("utf-8"), timeout=timeout)

    def _get(self, path: str, query: dict | None = None) -> Any:
        return self._request("GET", path, query=query)

    # -- external tasks ------------------------------------------------------

    def fetch_and_lock(self, worker_id: str, topic: str, lock_ms: int, max_tasks: int = 1,
                       long_poll_ms: int = 0, variables: list[str] | None = None) -> list[dict]:
        payload: dict[str, Any] = {
            "workerId": worker_id,
            "maxTasks": max_tasks,
            "usePriority": True,
            "topics": [{"topicName": topic, "lockDuration": lock_ms,
                        **({"variables": variables} if variables else {})}],
        }
        if long_poll_ms:
            payload["asyncResponseTimeout"] = long_poll_ms
        timeout = self.timeout + (long_poll_ms / 1000.0 if long_poll_ms else 0)
        return self._post_json("/external-task/fetchAndLock", payload, timeout=timeout) or []

    def complete(self, task_id: str, worker_id: str, variables: dict[str, dict]) -> None:
        self._post_json(f"/external-task/{task_id}/complete",
                        {"workerId": worker_id, "variables": variables})

    def failure(self, task_id: str, worker_id: str, message: str, details: str,
                retries: int, retry_timeout_ms: int) -> None:
        self._post_json(f"/external-task/{task_id}/failure",
                        {"workerId": worker_id, "errorMessage": message[:666],
                         "errorDetails": details[:4000], "retries": retries,
                         "retryTimeout": retry_timeout_ms})

    def extend_lock(self, task_id: str, worker_id: str, new_duration_ms: int) -> None:
        self._post_json(f"/external-task/{task_id}/extendLock",
                        {"workerId": worker_id, "newDuration": new_duration_ms})

    # -- deployment and instances ------------------------------------------------

    def deploy(self, name: str, bpmn_path: str | Path) -> dict:
        """Deploy one BPMN file (multipart/form-data). Duplicate filtering is on,
        so re-deploying an unchanged file creates no new version."""
        bpmn_path = Path(bpmn_path)
        boundary = "----openedge-" + uuid.uuid4().hex
        parts = [
            ("deployment-name", name), ("enable-duplicate-filtering", "true"),
            ("deploy-changed-only", "true"), ("deployment-source", "camunda_step/run.py"),
        ]
        body = b""
        for field, value in parts:
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n"
                     f"{value}\r\n").encode("utf-8")
        ctype = mimetypes.guess_type(bpmn_path.name)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{bpmn_path.name}\"; "
                 f"filename=\"{bpmn_path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
        body += bpmn_path.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        return self._request("POST", "/deployment/create", body,
                             content_type=f"multipart/form-data; boundary={boundary}")

    def start(self, key: str, variables: dict[str, Any], business_key: str | None = None) -> dict:
        payload: dict[str, Any] = {"variables": {k: typed(v) for k, v in variables.items()}}
        if business_key:
            payload["businessKey"] = business_key
        return self._post_json(f"/process-definition/key/{key}/start", payload)

    def instances(self, key: str) -> list[dict]:
        return self._get("/process-instance", {"processDefinitionKey": key}) or []

    def tasks(self, instance_id: str | None = None, key: str | None = None) -> list[dict]:
        return self._get("/task", {"processInstanceId": instance_id, "processDefinitionKey": key}) or []

    def variables(self, instance_id: str) -> dict[str, Any]:
        return plain(self._get(f"/process-instance/{instance_id}/variables") or {})

    # -- history: what the engine remembers ------------------------------------------

    def history_activities(self, instance_id: str) -> list[dict]:
        return self._get("/history/activity-instance",
                         {"processInstanceId": instance_id, "sortBy": "startTime", "sortOrder": "asc"}) or []

    def history_variables(self, instance_id: str) -> list[dict]:
        return self._get("/history/variable-instance", {"processInstanceId": instance_id}) or []

    def history_operations(self, instance_id: str) -> list[dict]:
        return self._get("/history/user-operation-log", {"processInstanceId": instance_id}) or []

    def history_instance(self, instance_id: str) -> dict | None:
        return self._get(f"/history/process-instance/{instance_id}")
