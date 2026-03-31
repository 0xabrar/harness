# App-Server Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans to implement this plan task-by-task.

**Goal:** Replace `codex exec` subprocess calls with the `codex app-server` JSON-RPC protocol, add `outputSchema` for structured role reports, support parallel execution via multiple app-server instances, and add proper lifecycle management.

**Architecture:** A new `harness_app_server.py` module provides a Python JSON-RPC client that spawns `codex app-server` as a child process and communicates via JSONL over stdin/stdout. A `ServerManager` class handles lazy spawning, task assignment, idle reaping, and cleanup. The existing `run_runtime` loop in `harness_runtime_ops.py` is modified to use the ServerManager instead of `subprocess.run(codex exec ...)`. Report delivery switches from file-based to `outputSchema` on `turn/start`, with the supervisor reading structured output from the turn result instead of disk. Role prompts are updated to remove file-write instructions. Parallel execution is supported by acquiring multiple app-server instances for independent ready tasks.

**Tech Stack:** Python 3.11+ stdlib only (json, subprocess, signal, atexit, time, threading). No new dependencies.

---

### Task 0: Add JSON-RPC client for codex app-server

**Files:**
- Create: `scripts/harness_app_server.py`
- Test: `tests/test_app_server.py`

**Step 1: Write the failing test for JSONL message framing**

```python
# tests/test_app_server.py
import json
import unittest

from harness_app_server import JsonRpcConnection


class TestJsonRpcConnection(unittest.TestCase):
    def test_build_request_message(self):
        conn = JsonRpcConnection.__new__(JsonRpcConnection)
        conn._next_id = 1
        msg = conn._build_request("initialize", {"clientInfo": {"name": "harness"}})
        parsed = json.loads(msg)
        self.assertEqual(parsed["jsonrpc"], "2.0")
        self.assertEqual(parsed["id"], 1)
        self.assertEqual(parsed["method"], "initialize")
        self.assertEqual(parsed["params"]["clientInfo"]["name"], "harness")

    def test_build_notification_message(self):
        conn = JsonRpcConnection.__new__(JsonRpcConnection)
        msg = conn._build_notification("initialized", {})
        parsed = json.loads(msg)
        self.assertEqual(parsed["jsonrpc"], "2.0")
        self.assertEqual(parsed["method"], "initialized")
        self.assertNotIn("id", parsed)

    def test_parse_response(self):
        conn = JsonRpcConnection.__new__(JsonRpcConnection)
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"thread": {"id": "t-1"}}})
        msg = conn._parse_line(line)
        self.assertEqual(msg["type"], "response")
        self.assertEqual(msg["id"], 1)
        self.assertEqual(msg["result"]["thread"]["id"], "t-1")

    def test_parse_notification(self):
        conn = JsonRpcConnection.__new__(JsonRpcConnection)
        line = json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
        msg = conn._parse_line(line)
        self.assertEqual(msg["type"], "notification")
        self.assertEqual(msg["method"], "turn/completed")

    def test_parse_error_response(self):
        conn = JsonRpcConnection.__new__(JsonRpcConnection)
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "not found"}})
        msg = conn._parse_line(line)
        self.assertEqual(msg["type"], "error")
        self.assertEqual(msg["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/code/harness && python -m pytest tests/test_app_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'harness_app_server'`

**Step 3: Implement the JSON-RPC client**

```python
# scripts/harness_app_server.py
"""JSON-RPC client for the codex app-server protocol."""
from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class AppServerError(RuntimeError):
    """Raised when the app-server protocol encounters an error."""
    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class JsonRpcConnection:
    """Low-level JSONL-over-stdin/stdout JSON-RPC transport."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._next_id = 1
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, dict] = {}
        self._notifications: list[dict] = []
        self._notification_lock = threading.Lock()
        self._closed = False
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _build_request(self, method: str, params: dict) -> str:
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self._next_id += 1
        return json.dumps(msg)

    def _build_notification(self, method: str, params: dict) -> str:
        return json.dumps({"jsonrpc": "2.0", "method": method, "params": params})

    def _parse_line(self, line: str) -> dict:
        msg = json.loads(line)
        if "id" in msg and "error" in msg:
            return {"type": "error", "id": msg["id"], "error": msg["error"]}
        if "id" in msg and "method" not in msg:
            return {"type": "response", "id": msg["id"], "result": msg.get("result", {})}
        if "method" in msg and "id" not in msg:
            return {"type": "notification", "method": msg["method"], "params": msg.get("params", {})}
        if "method" in msg and "id" in msg:
            return {"type": "server_request", "id": msg["id"], "method": msg["method"], "params": msg.get("params", {})}
        return {"type": "unknown", "raw": msg}

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        for raw_line in self._proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            parsed = self._parse_line(line)
            if parsed["type"] in ("response", "error"):
                msg_id = parsed["id"]
                self._results[msg_id] = parsed
                event = self._pending.get(msg_id)
                if event:
                    event.set()
            elif parsed["type"] == "notification":
                with self._notification_lock:
                    self._notifications.append(parsed)
            elif parsed["type"] == "server_request":
                # Reject unsupported server-initiated requests
                reject = json.dumps({
                    "jsonrpc": "2.0",
                    "id": parsed["id"],
                    "error": {"code": -32601, "message": f"Unsupported: {parsed['method']}"}
                })
                self._write(reject)

    def _write(self, message: str) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(message + "\n")
        self._proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 600.0) -> dict:
        msg = self._build_request(method, params)
        msg_id = self._next_id - 1
        event = threading.Event()
        self._pending[msg_id] = event
        self._write(msg)
        if not event.wait(timeout):
            self._pending.pop(msg_id, None)
            raise AppServerError(f"Timeout waiting for {method} response")
        self._pending.pop(msg_id, None)
        result = self._results.pop(msg_id)
        if result["type"] == "error":
            err = result["error"]
            raise AppServerError(err.get("message", "Unknown error"), code=err.get("code"), data=err)
        return result["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write(self._build_notification(method, params or {}))

    def drain_notifications(self) -> list[dict]:
        with self._notification_lock:
            batch = list(self._notifications)
            self._notifications.clear()
        return batch

    def wait_for_notification(self, method: str, timeout: float = 900.0, thread_id: str | None = None) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._notification_lock:
                for i, n in enumerate(self._notifications):
                    if n["method"] == method:
                        if thread_id and n["params"].get("threadId") != thread_id:
                            continue
                        self._notifications.pop(i)
                        return n
            time.sleep(0.05)
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None


class CodexAppServer:
    """High-level client for a single codex app-server process."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.pid: int | None = None
        self._conn: JsonRpcConnection | None = None

    def start(self) -> None:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            cwd=self.repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.pid = proc.pid
        self._conn = JsonRpcConnection(proc)
        self._conn.request("initialize", {
            "clientInfo": {"name": "harness", "title": "Codex Harness", "version": "1.0.0"},
            "capabilities": {"experimentalApi": False},
        })
        self._conn.notify("initialized")

    def start_thread(self, *, sandbox: str = "read-only", model: str | None = None, ephemeral: bool = True) -> str:
        assert self._conn is not None
        params: dict[str, Any] = {
            "cwd": str(self.repo),
            "approvalPolicy": "never",
            "sandbox": sandbox,
            "ephemeral": ephemeral,
        }
        if model:
            params["model"] = model
        result = self._conn.request("thread/start", params)
        return result["thread"]["id"]

    def resume_thread(self, thread_id: str, *, sandbox: str = "read-only", model: str | None = None) -> str:
        assert self._conn is not None
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(self.repo),
            "approvalPolicy": "never",
            "sandbox": sandbox,
        }
        if model:
            params["model"] = model
        result = self._conn.request("thread/resume", params)
        return result["thread"]["id"]

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        output_schema: dict | None = None,
        effort: str | None = None,
        model: str | None = None,
    ) -> dict:
        assert self._conn is not None
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
        }
        if output_schema:
            params["outputSchema"] = output_schema
        if effort:
            params["effort"] = effort
        if model:
            params["model"] = model
        self._conn.request("turn/start", params)

        # Wait for turn/completed on this thread
        completed = self._conn.wait_for_notification("turn/completed", thread_id=thread_id)
        notifications = self._conn.drain_notifications()

        final_message = ""
        file_changes: list[dict] = []
        command_executions: list[dict] = []
        reasoning: list[str] = []

        # Process all buffered notifications
        for n in notifications:
            item = n.get("params", {}).get("item", {})
            if item.get("type") == "agentMessage" and item.get("text"):
                final_message = item["text"]
            if item.get("type") == "fileChange":
                file_changes.append(item)
            if item.get("type") == "commandExecution":
                command_executions.append(item)
            if item.get("type") == "reasoning" and item.get("summary"):
                reasoning.append(str(item["summary"]))

        turn = completed["params"]["turn"] if completed else {"status": "unknown"}
        status = 0 if turn.get("status") == "completed" else 1

        return {
            "status": status,
            "thread_id": thread_id,
            "final_message": final_message,
            "file_changes": file_changes,
            "command_executions": command_executions,
            "reasoning_summary": reasoning,
            "turn": turn,
        }

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def alive(self) -> bool:
        return self._conn is not None and self._conn.alive


class ManagedServer:
    """An app-server instance tracked by the ServerManager."""

    def __init__(self, server: CodexAppServer):
        self.server = server
        self.current_task: str | None = None
        self.thread_history: dict[str, str] = {}  # task_id -> thread_id
        self.last_used: float = time.monotonic()

    def assign(self, task_id: str) -> None:
        self.current_task = task_id
        self.last_used = time.monotonic()

    def release(self) -> None:
        self.current_task = None
        self.last_used = time.monotonic()

    @property
    def idle(self) -> bool:
        return self.current_task is None

    @property
    def alive(self) -> bool:
        return self.server.alive


IDLE_TIMEOUT_SECONDS = 300  # 5 minutes
SERVERS_STATE_FILENAME = "harness-servers.json"


class ServerManager:
    """Manages a pool of codex app-server instances with lazy spawning and idle reaping."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._servers: list[ManagedServer] = []
        self._lock = threading.Lock()
        self._state_path = repo / SERVERS_STATE_FILENAME
        atexit.register(self.shutdown)
        self._register_signals()

    def _register_signals(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev = signal.getsignal(sig)
            def handler(signum, frame, _prev=prev):
                self.shutdown()
                if callable(_prev) and _prev not in (signal.SIG_DFL, signal.SIG_IGN):
                    _prev(signum, frame)
                elif _prev == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)
            signal.signal(sig, handler)

    def _persist_pids(self) -> None:
        pids = [ms.server.pid for ms in self._servers if ms.server.pid]
        try:
            self._state_path.write_text(json.dumps({"pids": pids}), encoding="utf-8")
        except Exception:
            pass

    def _reap_idle(self) -> None:
        now = time.monotonic()
        still_alive: list[ManagedServer] = []
        for ms in self._servers:
            if ms.idle and (now - ms.last_used) > IDLE_TIMEOUT_SECONDS:
                ms.server.close()
            elif not ms.alive:
                pass  # already dead, drop it
            else:
                still_alive.append(ms)
        self._servers = still_alive
        self._persist_pids()

    def acquire(self, task_id: str) -> ManagedServer:
        with self._lock:
            self._reap_idle()
            for ms in self._servers:
                if ms.idle and ms.alive:
                    ms.assign(task_id)
                    return ms
            # Spawn a new one
            server = CodexAppServer(self.repo)
            server.start()
            ms = ManagedServer(server)
            ms.assign(task_id)
            self._servers.append(ms)
            self._persist_pids()
            return ms

    def release(self, ms: ManagedServer) -> None:
        with self._lock:
            ms.release()

    def shutdown(self) -> None:
        with self._lock:
            for ms in self._servers:
                ms.server.close()
            self._servers.clear()
            try:
                self._state_path.unlink(missing_ok=True)
            except Exception:
                pass

    def kill_orphans(self) -> None:
        """Kill app-server processes from a previous crashed harness run."""
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            for pid in data.get("pids", []):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, ValueError):
                    pass
            self._state_path.unlink(missing_ok=True)
        except Exception:
            pass
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_app_server.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
cd /home/dev/code/harness
git add scripts/harness_app_server.py tests/test_app_server.py
git commit -m "feat: add JSON-RPC client for codex app-server protocol

Introduces CodexAppServer (single process client), JsonRpcConnection
(JSONL transport), ManagedServer (assignment tracking), and
ServerManager (pool with lazy spawn, idle reap, signal cleanup)."
```

---

### Task 1: Add output schemas for role reports

**Files:**
- Create: `schemas/planner-report.schema.json`
- Create: `schemas/implementer-report.schema.json`
- Create: `schemas/verifier-report.schema.json`
- Create: `scripts/harness_schemas.py`
- Test: `tests/test_schemas.py`

**Step 1: Write the failing test**

```python
# tests/test_schemas.py
import json
import unittest

from harness_schemas import load_schema, validate_report


class TestSchemas(unittest.TestCase):
    def test_load_planner_schema(self):
        schema = load_schema("planner")
        self.assertEqual(schema["type"], "object")
        self.assertIn("role", schema["properties"])

    def test_load_implementer_schema(self):
        schema = load_schema("implementer")
        self.assertIn("commit", schema["properties"])

    def test_load_verifier_schema(self):
        schema = load_schema("verifier")
        self.assertIn("verdict", schema["properties"])

    def test_validate_valid_planner_report(self):
        report = {
            "role": "planner",
            "revision": 1,
            "summary": "Initial plan",
            "task_changes": {"added": [], "updated": [], "closed": []},
            "planner_requested_reason": "initial_plan",
        }
        self.assertTrue(validate_report(report, "planner"))

    def test_validate_valid_verifier_report(self):
        report = {
            "role": "verifier",
            "task_id": "T-001",
            "attempt": 1,
            "commit": "abc1234",
            "verdict": "accept",
            "summary": "All good",
            "findings": [],
            "criteria_results": [],
            "proposed_tasks": [],
        }
        self.assertTrue(validate_report(report, "verifier"))

    def test_validate_invalid_verdict_rejected(self):
        report = {
            "role": "verifier",
            "task_id": "T-001",
            "attempt": 1,
            "commit": "abc1234",
            "verdict": "maybe",
            "summary": "Unsure",
            "findings": [],
            "criteria_results": [],
            "proposed_tasks": [],
        }
        self.assertFalse(validate_report(report, "verifier"))


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'harness_schemas'`

**Step 3: Create the JSON schemas**

```json
// schemas/planner-report.schema.json
{
  "type": "object",
  "required": ["role", "revision", "summary", "task_changes", "planner_requested_reason"],
  "properties": {
    "role": {"type": "string", "const": "planner"},
    "revision": {"type": "integer"},
    "summary": {"type": "string"},
    "task_changes": {
      "type": "object",
      "required": ["added", "updated", "closed"],
      "properties": {
        "added": {"type": "array", "items": {"type": "string"}},
        "updated": {"type": "array", "items": {"type": "string"}},
        "closed": {"type": "array", "items": {"type": "string"}}
      }
    },
    "planner_requested_reason": {"type": "string"}
  },
  "additionalProperties": false
}
```

```json
// schemas/implementer-report.schema.json
{
  "type": "object",
  "required": ["role", "task_id", "attempt", "commit", "summary", "files_changed", "checks_run", "proposed_tasks"],
  "properties": {
    "role": {"type": "string", "const": "implementer"},
    "task_id": {"type": "string"},
    "attempt": {"type": "integer"},
    "commit": {"type": "string"},
    "summary": {"type": "string"},
    "files_changed": {"type": "array", "items": {"type": "string"}},
    "checks_run": {"type": "array", "items": {"type": "string"}},
    "proposed_tasks": {"type": "array"}
  },
  "additionalProperties": false
}
```

```json
// schemas/verifier-report.schema.json
{
  "type": "object",
  "required": ["role", "task_id", "attempt", "commit", "verdict", "summary", "findings", "criteria_results", "proposed_tasks"],
  "properties": {
    "role": {"type": "string", "const": "verifier"},
    "task_id": {"type": "string"},
    "attempt": {"type": "integer"},
    "commit": {"type": "string"},
    "verdict": {"type": "string", "enum": ["accept", "revert", "needs_human"]},
    "summary": {"type": "string"},
    "findings": {"type": "array"},
    "criteria_results": {"type": "array"},
    "proposed_tasks": {"type": "array"}
  },
  "additionalProperties": false
}
```

**Step 4: Implement the schema loader**

```python
# scripts/harness_schemas.py
"""Load and validate role report schemas for outputSchema delivery."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

_SCHEMA_FILES = {
    "planner": "planner-report.schema.json",
    "implementer": "implementer-report.schema.json",
    "verifier": "verifier-report.schema.json",
}


def load_schema(role: str) -> dict[str, Any]:
    filename = _SCHEMA_FILES.get(role)
    if not filename:
        raise ValueError(f"No schema for role: {role}")
    path = SCHEMAS_DIR / filename
    return json.loads(path.read_text(encoding="utf-8"))


def validate_report(report: dict[str, Any], role: str) -> bool:
    """Basic structural validation (no jsonschema dependency)."""
    schema = load_schema(role)
    required = schema.get("required", [])
    for field in required:
        if field not in report:
            return False
    props = schema.get("properties", {})
    for key, spec in props.items():
        if key not in report:
            continue
        if "const" in spec and report[key] != spec["const"]:
            return False
        if "enum" in spec and report[key] not in spec["enum"]:
            return False
        if "type" in spec:
            expected_type = {"string": str, "integer": int, "array": list, "object": dict}.get(spec["type"])
            if expected_type and not isinstance(report[key], expected_type):
                return False
    return True
```

**Step 5: Run tests to verify they pass**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_schemas.py -v`
Expected: All 6 tests PASS

**Step 6: Commit**

```bash
cd /home/dev/code/harness
git add schemas/ scripts/harness_schemas.py tests/test_schemas.py
git commit -m "feat: add JSON output schemas for planner/implementer/verifier reports

These schemas are passed as outputSchema to codex app-server turn/start,
replacing the file-based report delivery mechanism."
```

---

### Task 2: Update role prompts to use outputSchema instead of file-based reports

**Files:**
- Modify: `scripts/harness_build_prompt.py`
- Test: `tests/test_build_prompt.py`

**Step 1: Write the failing test**

```python
# tests/test_build_prompt.py
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_build_prompt import build_planner_prompt, build_implementer_prompt, build_verifier_prompt


class TestPromptChanges(unittest.TestCase):
    """Verify prompts no longer reference report file paths."""

    @patch("harness_build_prompt.state_context")
    def test_planner_prompt_no_report_path(self, mock_ctx):
        mock_ctx.return_value = (
            {"config": {"goal": "test", "scope": "."}, "state": {"planner_revision": 0}},
            {"planner_revision": 0, "tasks": []},
        )
        paths = _fake_paths()
        prompt = build_planner_prompt(paths)
        self.assertNotIn("reports/planner", prompt)
        self.assertIn("Return your report as structured JSON", prompt)

    @patch("harness_build_prompt.state_context")
    def test_implementer_prompt_no_report_path(self, mock_ctx):
        mock_ctx.return_value = (
            {"config": {"goal": "test", "scope": "."}, "state": {"current_task_id": "1", "current_attempt": 1}},
            {"planner_revision": 0, "tasks": [
                {"id": "1", "title": "Test", "description": "Do test", "acceptance_criteria": ["pass"],
                 "status": "ready", "priority": 1, "dependencies": [], "attempts": 0}
            ]},
        )
        paths = _fake_paths()
        prompt = build_implementer_prompt(paths)
        self.assertNotIn("Write the implementer report to", prompt)
        self.assertIn("Return your report as structured JSON", prompt)

    @patch("harness_build_prompt.read_json")
    def test_verifier_prompt_no_report_path(self, mock_read):
        mock_read.return_value = {
            "config": {"goal": "test", "scope": "."},
            "state": {"current_task_id": "1", "current_attempt": 1, "trial_commit": "abc123"},
        }
        paths = _fake_paths()
        with patch("harness_build_prompt.refresh_ready_tasks") as mock_refresh, \
             patch("harness_build_prompt.load_tasks") as mock_load:
            mock_load.return_value = {"tasks": [
                {"id": "1", "title": "Test", "description": "Do test", "acceptance_criteria": ["pass"],
                 "status": "in_progress", "priority": 1, "dependencies": [], "attempts": 1}
            ]}
            mock_refresh.return_value = mock_load.return_value
            prompt = build_verifier_prompt(paths)
        self.assertNotIn("Write a verifier report to", prompt)
        self.assertIn("Return your report as structured JSON", prompt)


def _fake_paths():
    from harness_artifacts import Paths
    base = Path("/tmp/fake-repo")
    return Paths(
        repo=base, launch=base / "harness-launch.json", runtime=base / "harness-runtime.json",
        runtime_log=base / "harness-runtime.log", state=base / "harness-state.json",
        events=base / "harness-events.tsv", lessons=base / "harness-lessons.md",
        plan=base / "plan.md", tasks=base / "tasks.json", reports=base / "reports",
    )


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_build_prompt.py -v`
Expected: FAIL — prompts still contain report file paths

**Step 3: Update the prompts in harness_build_prompt.py**

Remove all references to writing report files. Replace with instructions to return structured JSON as the final response. The prompts should tell each role: "Return your report as structured JSON matching the schema. Do not write any report files."

Key changes per role:
- **Planner:** Remove `Write a planner report to {report_path}`. Add `Return your report as structured JSON. The runtime will capture it via outputSchema.`
- **Implementer:** Remove `Write the implementer report to {report_path}`. Add same structured JSON instruction.
- **Verifier:** Remove `Write a verifier report to {report_path}`. Add same structured JSON instruction.

Keep all other prompt content (goal, scope, task details, acceptance criteria, role constraints) unchanged.

**Step 4: Run tests to verify they pass**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_build_prompt.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
cd /home/dev/code/harness
git add scripts/harness_build_prompt.py tests/test_build_prompt.py
git commit -m "refactor: update role prompts to use outputSchema instead of file-based reports

Roles now return structured JSON as their final response rather than
writing report files to disk. The runtime captures reports via the
app-server outputSchema mechanism."
```

---

### Task 3: Update supervisor to read reports from turn results instead of disk

**Files:**
- Modify: `scripts/harness_supervisor_status.py`
- Create: `scripts/harness_report_parser.py`
- Test: `tests/test_report_parser.py`

**Step 1: Write the failing test**

```python
# tests/test_report_parser.py
import json
import unittest

from harness_report_parser import parse_structured_output


class TestReportParser(unittest.TestCase):
    def test_parse_valid_json(self):
        raw = json.dumps({"role": "verifier", "verdict": "accept", "summary": "ok"})
        result = parse_structured_output(raw)
        self.assertIsNotNone(result["parsed"])
        self.assertIsNone(result["parse_error"])
        self.assertEqual(result["parsed"]["verdict"], "accept")

    def test_parse_empty_returns_error(self):
        result = parse_structured_output("")
        self.assertIsNone(result["parsed"])
        self.assertIsNotNone(result["parse_error"])

    def test_parse_invalid_json_returns_error(self):
        result = parse_structured_output("not json {{{")
        self.assertIsNone(result["parsed"])
        self.assertIsNotNone(result["parse_error"])

    def test_parse_json_in_markdown_fence(self):
        raw = '```json\n{"role": "planner", "revision": 1}\n```'
        result = parse_structured_output(raw)
        self.assertIsNotNone(result["parsed"])
        self.assertEqual(result["parsed"]["role"], "planner")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_report_parser.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Implement the report parser**

```python
# scripts/harness_report_parser.py
"""Parse structured output from app-server turn results."""
from __future__ import annotations

import json
import re
from typing import Any


def parse_structured_output(raw: str | None) -> dict[str, Any]:
    """Parse a role report from the app-server's final message.

    Handles: raw JSON, JSON inside markdown fences, empty/missing output.
    """
    if not raw or not raw.strip():
        return {"parsed": None, "parse_error": "Empty output from Codex turn.", "raw": raw or ""}

    text = raw.strip()

    # Try direct JSON parse
    try:
        return {"parsed": json.loads(text), "parse_error": None, "raw": raw}
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown fence
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return {"parsed": json.loads(match.group(1).strip()), "parse_error": None, "raw": raw}
        except json.JSONDecodeError:
            pass

    return {"parsed": None, "parse_error": f"Could not parse JSON from output: {text[:200]}", "raw": raw}
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_report_parser.py -v`
Expected: All 4 tests PASS

**Step 5: Update harness_supervisor_status.py**

Modify `evaluate_supervisor_status` to accept an optional `turn_result` parameter containing the parsed report from the app-server turn. When provided, use it instead of reading from disk. When not provided (backward compat), fall back to reading report files.

Key change: each of `planner_report_state`, `implementer_report_state`, `verifier_report_state` gets an optional `report_override: dict | None` parameter. If provided, skip the `read_json(report_path)` call and use the override.

Also: when a report comes via `turn_result`, still write it to the report file for auditability (the events TSV and lessons need the data, and humans may want to inspect reports).

**Step 6: Run existing tests + new tests**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/ -v`
Expected: All tests PASS

**Step 7: Commit**

```bash
cd /home/dev/code/harness
git add scripts/harness_report_parser.py scripts/harness_supervisor_status.py tests/test_report_parser.py
git commit -m "feat: supervisor accepts reports from turn results with disk fallback

Reports can now be delivered via outputSchema (parsed from turn result)
or read from disk (backward compat). Reports are always persisted to
disk for auditability regardless of delivery mechanism."
```

---

### Task 4: Replace codex exec with app-server in the runtime loop

**Files:**
- Modify: `scripts/harness_runtime_ops.py`
- Modify: `scripts/harness_runtime_common.py`
- Test: `tests/test_runtime_ops.py`

**Step 1: Write the failing test**

```python
# tests/test_runtime_ops.py
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from harness_runtime_ops import run_role_turn


class TestRunRoleTurn(unittest.TestCase):
    @patch("harness_runtime_ops.ServerManager")
    @patch("harness_runtime_ops.load_schema")
    def test_run_role_turn_returns_parsed_report(self, mock_schema, mock_mgr_cls):
        mock_schema.return_value = {"type": "object", "properties": {}}
        ms = MagicMock()
        ms.server.start_thread.return_value = "thread-1"
        ms.server.run_turn.return_value = {
            "status": 0,
            "thread_id": "thread-1",
            "final_message": '{"role": "planner", "revision": 1, "summary": "ok"}',
        }
        mock_mgr = MagicMock()
        mock_mgr.acquire.return_value = ms
        result = run_role_turn(
            manager=mock_mgr,
            role="planner",
            task_id="",
            prompt="test prompt",
            repo=Path("/tmp/test"),
            sandbox="workspace-write",
        )
        self.assertEqual(result["report"]["role"], "planner")
        mock_mgr.release.assert_called_once_with(ms)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_runtime_ops.py::TestRunRoleTurn -v`
Expected: FAIL — `run_role_turn` doesn't exist yet

**Step 3: Implement run_role_turn and update run_runtime**

Add to `harness_runtime_ops.py`:

```python
def run_role_turn(
    *,
    manager: ServerManager,
    role: str,
    task_id: str,
    prompt: str,
    repo: Path,
    sandbox: str,
    resume_thread_id: str | None = None,
) -> dict[str, Any]:
    """Execute a single role turn via the app-server."""
    ms = manager.acquire(task_id or role)
    try:
        schema = load_schema(role)
        if resume_thread_id:
            thread_id = ms.server.resume_thread(resume_thread_id, sandbox=sandbox)
        else:
            thread_id = ms.server.start_thread(sandbox=sandbox)
        result = ms.server.run_turn(thread_id, prompt, output_schema=schema)
        ms.thread_history[task_id or role] = thread_id

        parsed = parse_structured_output(result.get("final_message", ""))
        report = parsed["parsed"]
        if report is None:
            raise HarnessError(f"Failed to parse {role} report: {parsed['parse_error']}")

        return {
            "report": report,
            "thread_id": thread_id,
            "turn_result": result,
            "parse_error": parsed["parse_error"],
        }
    except (BrokenPipeError, ConnectionError, EOFError) as exc:
        # App-server crashed — remove dead server, retry with fresh one
        manager._servers = [s for s in manager._servers if s is not ms]
        ms_new = manager.acquire(task_id or role)
        try:
            thread_id = ms_new.server.start_thread(sandbox=sandbox)
            result = ms_new.server.run_turn(thread_id, prompt, output_schema=load_schema(role))
            parsed = parse_structured_output(result.get("final_message", ""))
            report = parsed["parsed"]
            if report is None:
                raise HarnessError(f"Failed to parse {role} report after retry: {parsed['parse_error']}")
            return {"report": report, "thread_id": thread_id, "turn_result": result, "parse_error": None}
        finally:
            manager.release(ms_new)
    finally:
        manager.release(ms)
```

Update `run_runtime` to:
1. Create a `ServerManager` at loop start
2. Replace `subprocess.run(codex_cmd, ...)` with `run_role_turn(manager=..., role=..., ...)`
3. Map role to sandbox: planner/implementer → `"workspace-write"`, verifier → `"read-only"`
4. Pass the parsed report to `evaluate_supervisor_status` via the new `report_override` parameter
5. Still write report to disk for auditability
6. Call `manager.shutdown()` on exit

Remove `build_codex_exec_command` (no longer needed).

Update `harness_runtime_common.py`:
- Remove `codex_args_for_execution_policy` (no longer needed — sandbox mode replaces CLI flags)
- Keep `persist_runtime`, `ensure_runtime_not_running`, `runtime_summary` unchanged

**Step 4: Run tests to verify they pass**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/ -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /home/dev/code/harness
git add scripts/harness_runtime_ops.py scripts/harness_runtime_common.py tests/test_runtime_ops.py
git commit -m "feat: replace codex exec with app-server JSON-RPC in runtime loop

The runtime now uses ServerManager to acquire app-server instances,
runs role turns via thread/start + turn/start, and receives reports
through outputSchema. Sandbox mode is set per-role: workspace-write
for planner/implementer, read-only for verifier. Crash recovery
retries with a fresh app-server on pipe errors."
```

---

### Task 5: Add parallel execution for independent ready tasks

**Files:**
- Modify: `scripts/harness_runtime_ops.py`
- Modify: `scripts/harness_artifacts.py` (add `all_ready_tasks`)
- Test: `tests/test_parallel.py`

**Step 1: Write the failing test**

```python
# tests/test_parallel.py
import unittest
from harness_artifacts import all_ready_tasks, refresh_ready_tasks


class TestAllReadyTasks(unittest.TestCase):
    def test_returns_multiple_ready_tasks(self):
        payload = {
            "version": 1,
            "goal": "test",
            "planner_revision": 1,
            "tasks": [
                {"id": "1", "title": "A", "description": "a", "acceptance_criteria": [],
                 "status": "ready", "priority": 1, "dependencies": []},
                {"id": "2", "title": "B", "description": "b", "acceptance_criteria": [],
                 "status": "ready", "priority": 2, "dependencies": []},
                {"id": "3", "title": "C", "description": "c", "acceptance_criteria": [],
                 "status": "pending", "priority": 1, "dependencies": ["1"]},
            ],
        }
        payload = refresh_ready_tasks(payload)
        ready = all_ready_tasks(payload)
        self.assertEqual(len(ready), 2)
        self.assertEqual(ready[0]["id"], "1")
        self.assertEqual(ready[1]["id"], "2")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_parallel.py -v`
Expected: FAIL — `all_ready_tasks` doesn't exist

**Step 3: Implement all_ready_tasks**

Add to `harness_artifacts.py`:

```python
def all_ready_tasks(tasks_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all tasks with status 'ready', sorted by priority then id."""
    payload = refresh_ready_tasks(tasks_payload)
    ready = [task for task in payload["tasks"] if task["status"] == "ready"]
    ready.sort(key=lambda task: (int(task.get("priority", 100)), str(task["id"])))
    return [deepcopy(t) for t in ready]
```

**Step 4: Update run_runtime for parallel execution**

In `harness_runtime_ops.py`, after the planner runs, check if multiple tasks are ready. If so, run implementers in parallel using `threading.Thread` (one per ready task, each acquires its own app-server from the manager). Wait for all to complete. Then run verifiers in parallel for all completed implementations.

Key logic:
```python
ready_tasks = all_ready_tasks(tasks_payload)
if len(ready_tasks) > 1:
    # Parallel: run all implementers concurrently
    results = {}
    threads = []
    for task in ready_tasks:
        t = threading.Thread(target=_run_impl_task, args=(manager, task, paths, results))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    # Process results sequentially (state transitions must be serial)
    for task_id, result in results.items():
        # apply supervisor transition per task
else:
    # Single task — existing sequential flow
```

**Step 5: Run tests to verify they pass**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
cd /home/dev/code/harness
git add scripts/harness_runtime_ops.py scripts/harness_artifacts.py tests/test_parallel.py
git commit -m "feat: add parallel execution for independent ready tasks

When the planner produces multiple independent tasks (no mutual deps),
the runtime runs implementers concurrently, each with its own app-server
instance. State transitions remain serial after all turns complete."
```

---

### Task 6: Add thread resume for implementer retries

**Files:**
- Modify: `scripts/harness_runtime_ops.py`
- Test: Update `tests/test_runtime_ops.py`

**Step 1: Write the failing test**

```python
# Add to tests/test_runtime_ops.py
class TestThreadResume(unittest.TestCase):
    @patch("harness_runtime_ops.ServerManager")
    @patch("harness_runtime_ops.load_schema")
    def test_retry_uses_resume_thread(self, mock_schema, mock_mgr_cls):
        mock_schema.return_value = {"type": "object", "properties": {}}
        ms = MagicMock()
        ms.thread_history = {"T-001": "thread-prev"}
        ms.server.resume_thread.return_value = "thread-prev"
        ms.server.run_turn.return_value = {
            "status": 0,
            "thread_id": "thread-prev",
            "final_message": '{"role": "implementer", "task_id": "T-001", "attempt": 2, "commit": "def456"}',
        }
        mock_mgr = MagicMock()
        mock_mgr.acquire.return_value = ms
        result = run_role_turn(
            manager=mock_mgr,
            role="implementer",
            task_id="T-001",
            prompt="Retry: your previous commit was reverted because...",
            repo=Path("/tmp/test"),
            sandbox="workspace-write",
            resume_thread_id="thread-prev",
        )
        ms.server.resume_thread.assert_called_once_with("thread-prev", sandbox="workspace-write")
        self.assertEqual(result["report"]["attempt"], 2)
```

**Step 2: Run test to verify it fails**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_runtime_ops.py::TestThreadResume -v`
Expected: FAIL (or PASS if Task 4 already handles resume — verify)

**Step 3: Wire thread resume into the runtime loop**

In the `run_runtime` loop, when the verifier returns `revert` and the task is being retried:
1. Look up the previous thread ID from `ms.thread_history[task_id]`
2. Pass it as `resume_thread_id` to `run_role_turn`
3. Prepend to the implementer prompt: "Your previous commit {trial_commit} was reverted. Verifier feedback: {verdict_summary}. Try again."

If the previous app-server died (thread not available), fall back to a fresh thread.

**Step 4: Run tests to verify they pass**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/ -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /home/dev/code/harness
git add scripts/harness_runtime_ops.py tests/test_runtime_ops.py
git commit -m "feat: resume implementer thread on retry after verifier revert

When retrying a reverted task, the runtime resumes the implementer's
previous thread so it retains context of what was tried. Falls back
to a fresh thread if the previous app-server is unavailable."
```

---

### Task 7: Update SKILL.md and references for new runtime

**Files:**
- Modify: `SKILL.md`
- Modify: `references/runtime-control.md`
- Modify: `references/role-contracts.md`
- Modify: `references/report-schemas.md`

**Step 1: Update SKILL.md**

- Update the runtime description to mention app-server protocol instead of `codex exec`
- Add note about parallel execution capability
- Update artifact list to include `harness-servers.json`
- Remove references to execution policy CLI flags

**Step 2: Update references/runtime-control.md**

- Replace `codex exec` subprocess documentation with app-server lifecycle
- Document ServerManager behavior (lazy spawn, idle reap, signal cleanup)
- Document the `harness-servers.json` orphan cleanup mechanism

**Step 3: Update references/role-contracts.md**

- Remove instructions about writing report files to disk
- Add: "Return your report as structured JSON. The runtime captures it via outputSchema."
- Document sandbox modes per role

**Step 4: Update references/report-schemas.md**

- Note that schemas are now enforced via `outputSchema` on `turn/start`
- Point to `schemas/*.schema.json` as the canonical schema definitions
- Keep the example JSON for reference

**Step 5: Commit**

```bash
cd /home/dev/code/harness
git add SKILL.md references/
git commit -m "docs: update skill and reference docs for app-server migration

Reflects the switch from codex exec to app-server JSON-RPC, outputSchema
report delivery, per-role sandbox modes, and parallel execution."
```

---

### Task 8: Integration test — end-to-end runtime loop

**Files:**
- Create: `tests/test_integration.py`

**Step 1: Write the integration test**

Create a test that mocks the `CodexAppServer` class to simulate a full planner → implementer → verifier → accept cycle without hitting real Codex. Verify:
- ServerManager spawns/reuses/releases servers correctly
- Reports are parsed from turn results
- Supervisor transitions work (planner → implementer → verifier → stop)
- Thread IDs are tracked in `thread_history`
- PID state file is written and cleaned up

**Step 2: Run the integration test**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/test_integration.py -v`
Expected: PASS

**Step 3: Commit**

```bash
cd /home/dev/code/harness
git add tests/test_integration.py
git commit -m "test: add integration test for full app-server runtime loop

Simulates planner -> implementer -> verifier -> accept cycle with
mocked app-server, verifying state transitions, report parsing,
server lifecycle, and thread tracking."
```

---

### Task 9: Clean up dead code

**Files:**
- Modify: `scripts/harness_runtime_ops.py`
- Modify: `scripts/harness_runtime_common.py`

**Step 1: Remove dead code**

- Remove `build_codex_exec_command` from `harness_runtime_ops.py` (if not already removed in Task 4)
- Remove `codex_args_for_execution_policy` from `harness_runtime_common.py` (if not already removed)
- Remove `command_is_executable` if no longer used
- Remove any imports that are no longer needed
- Remove `EXECUTION_POLICY_CHOICES` from `harness_artifacts.py` if the concept is fully replaced by sandbox modes

**Step 2: Run all tests**

Run: `cd /home/dev/code/harness && PYTHONPATH=scripts python -m pytest tests/ -v`
Expected: All tests PASS, no import errors

**Step 3: Commit**

```bash
cd /home/dev/code/harness
git add scripts/ 
git commit -m "chore: remove codex exec dead code after app-server migration

Removes build_codex_exec_command, codex_args_for_execution_policy,
command_is_executable, and related imports that are no longer used."
```
