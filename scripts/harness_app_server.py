#!/usr/bin/env python3
"""JSON-RPC client for the codex app-server protocol.

Provides a Python equivalent of the Node.js reference implementation at
codex-plugin-cc/plugins/codex/scripts/lib/app-server.mjs.  Communicates
over JSONL (newline-delimited JSON) on subprocess stdin/stdout.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDLE_TIMEOUT_SECONDS: int = 300
SERVERS_STATE_FILENAME: str = "harness-servers.json"

_DEFAULT_CLIENT_INFO: dict[str, str] = {
    "title": "Codex Harness",
    "name": "codex-harness",
    "version": "0.1.0",
}

_DEFAULT_CAPABILITIES: dict[str, Any] = {
    "experimentalApi": False,
    "optOutNotificationMethods": [
        "item/agentMessage/delta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/textDelta",
    ],
}


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class AppServerError(Exception):
    """Error raised by the app-server protocol layer."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code: int | None = code
        self.data: Any = data


# ---------------------------------------------------------------------------
# JsonRpcConnection — low-level JSONL transport
# ---------------------------------------------------------------------------

class JsonRpcConnection:
    """Low-level JSONL transport over a subprocess stdin/stdout pair."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        self._next_id: int = 1
        self._lock = threading.Lock()

        # Pending requests: id -> (Event, result_holder)
        # result_holder is a one-element list: [value | AppServerError]
        self._pending: dict[int, tuple[threading.Event, list[Any]]] = {}

        # Notification buffer
        self._notifications: list[dict[str, Any]] = []
        self._notification_lock = threading.Lock()

        # Waiters for specific notifications: list of (method, thread_id, Event, holder)
        self._notification_waiters: list[tuple[str | None, str | None, threading.Event, list[Any]]] = []
        self._waiter_lock = threading.Lock()

        # Background reader
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    # -- message builders --------------------------------------------------

    def _build_request(self, method: str, params: dict[str, Any] | None = None) -> str:
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        return json.dumps(payload, separators=(",", ":"))

    def _build_notification(self, method: str, params: dict[str, Any] | None = None) -> str:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        return json.dumps(payload, separators=(",", ":"))

    # -- line parser -------------------------------------------------------

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any]:
        """Parse a single JSONL line from the server.

        Returns a dict with a ``type`` key — one of:
        ``"response"`` | ``"notification"`` | ``"error"`` | ``"server_request"``.
        """
        stripped = line.strip()
        if not stripped:
            return {"type": "empty"}
        data = json.loads(stripped)

        # Server request: has both id and method
        if "id" in data and "method" in data:
            return {"type": "server_request", "id": data["id"], "method": data["method"], "params": data.get("params")}

        # Response (success or error): has id, no method
        if "id" in data:
            if "error" in data:
                err = data["error"]
                return {
                    "type": "error",
                    "id": data["id"],
                    "error": err,
                }
            return {
                "type": "response",
                "id": data["id"],
                "result": data.get("result", {}),
            }

        # Notification: has method, no id
        if "method" in data:
            return {
                "type": "notification",
                "method": data["method"],
                "params": data.get("params", {}),
            }

        return {"type": "unknown", "raw": data}

    # -- background reader -------------------------------------------------

    def _read_loop(self) -> None:
        """Read stdout line-by-line until EOF, dispatching messages."""
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for raw_line in stdout:
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    parsed = self._parse_line(line)
                except json.JSONDecodeError:
                    continue

                msg_type = parsed.get("type")

                if msg_type == "response":
                    self._resolve_pending(parsed["id"], parsed.get("result", {}))
                elif msg_type == "error":
                    err = parsed["error"]
                    exc = AppServerError(
                        err.get("message", "app-server error"),
                        code=err.get("code"),
                        data=err,
                    )
                    self._resolve_pending(parsed["id"], exc)
                elif msg_type == "server_request":
                    self._handle_server_request(parsed)
                elif msg_type == "notification":
                    self._dispatch_notification(parsed)
        except (ValueError, OSError):
            pass
        finally:
            eof_err = AppServerError("app-server connection closed")
            # Reject all pending requests on EOF
            with self._lock:
                for _id, (event, holder) in self._pending.items():
                    if not event.is_set():
                        holder.append(eof_err)
                        event.set()
                self._pending.clear()
            # Wake all notification waiters so run_turn doesn't hang
            with self._waiter_lock:
                for _, _, w_event, w_holder in self._notification_waiters:
                    if not w_event.is_set():
                        w_holder.append({"method": "__eof__", "params": {"error": "app-server connection closed"}})
                        w_event.set()
                self._notification_waiters.clear()

    def _resolve_pending(self, msg_id: int, value: Any) -> None:
        with self._lock:
            entry = self._pending.pop(msg_id, None)
        if entry is None:
            return
        event, holder = entry
        holder.append(value)
        event.set()

    def _handle_server_request(self, parsed: dict[str, Any]) -> None:
        """Reply with 'method not supported' for any server-initiated request."""
        response = json.dumps({
            "id": parsed["id"],
            "error": {"code": -32601, "message": f"Unsupported server request: {parsed['method']}"},
        }, separators=(",", ":"))
        self._send_raw(response + "\n")

    def _dispatch_notification(self, parsed: dict[str, Any]) -> None:
        method = parsed.get("method", "")
        params = parsed.get("params", {})
        notif = {"method": method, "params": params}

        # Check waiters first
        with self._waiter_lock:
            matched_idx: int | None = None
            for idx, (w_method, w_thread_id, w_event, w_holder) in enumerate(self._notification_waiters):
                if w_method is not None and w_method != method:
                    continue
                if w_thread_id is not None and params.get("threadId") != w_thread_id:
                    continue
                matched_idx = idx
                break
            if matched_idx is not None:
                _, _, w_event, w_holder = self._notification_waiters.pop(matched_idx)
                w_holder.append(notif)
                w_event.set()
                return

        # Buffer it
        with self._notification_lock:
            self._notifications.append(notif)

    # -- public API --------------------------------------------------------

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 600) -> Any:
        """Send a JSON-RPC request and block until the response arrives."""
        line = self._build_request(method, params)
        # Extract id we just assigned
        msg_id = json.loads(line)["id"]
        event = threading.Event()
        holder: list[Any] = []
        with self._lock:
            self._pending[msg_id] = (event, holder)
        self._send_raw(line + "\n")
        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise AppServerError(f"Timeout waiting for response to {method} (id={msg_id})")
        result = holder[0] if holder else AppServerError("No response received")
        if isinstance(result, AppServerError):
            raise result
        return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a fire-and-forget notification."""
        line = self._build_notification(method, params)
        self._send_raw(line + "\n")

    def drain_notifications(self) -> list[dict[str, Any]]:
        """Return and clear all buffered notifications."""
        with self._notification_lock:
            buffered = list(self._notifications)
            self._notifications.clear()
        return buffered

    def _match_notification(self, notif: dict, method: str | None, thread_id: str | None) -> bool:
        if method is not None and notif.get("method") != method:
            return False
        if thread_id is not None and notif.get("params", {}).get("threadId") != thread_id:
            return False
        return True

    def wait_for_notification(
        self,
        method: str | None = None,
        timeout: float = 600,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Block until a notification matching *method* and/or *thread_id* arrives."""
        # Atomically check buffer AND install waiter to prevent race
        event = threading.Event()
        holder: list[Any] = []
        with self._waiter_lock:
            with self._notification_lock:
                for idx, notif in enumerate(self._notifications):
                    if self._match_notification(notif, method, thread_id):
                        return self._notifications.pop(idx)
            self._notification_waiters.append((method, thread_id, event, holder))
        if not event.wait(timeout=timeout):
            # Remove our waiter on timeout
            with self._waiter_lock:
                self._notification_waiters = [
                    w for w in self._notification_waiters if w[2] is not event
                ]
            raise AppServerError(f"Timeout waiting for notification {method!r} (thread_id={thread_id!r})")
        if not holder:
            raise AppServerError("No notification received")
        return holder[0]

    def close(self) -> None:
        """Terminate the subprocess."""
        proc = self._proc
        if proc.poll() is None:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except OSError:
                pass
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # -- internal ----------------------------------------------------------

    def _send_raw(self, data: str) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            raise AppServerError("app-server stdin is not available")
        try:
            stdin.write(data)
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError(f"Failed to write to app-server stdin: {exc}") from exc


# ---------------------------------------------------------------------------
# CodexAppServer — high-level client for one codex app-server process
# ---------------------------------------------------------------------------

class CodexAppServer:
    """High-level client wrapping a single ``codex app-server`` process."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        codex_bin: str = "codex",
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._env = env
        self._codex_bin = codex_bin
        self._conn: JsonRpcConnection | None = None
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        """Spawn ``codex app-server`` and complete the initialize handshake."""
        self._proc = subprocess.Popen(
            [self._codex_bin, "app-server"],
            cwd=self._cwd,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._conn = JsonRpcConnection(self._proc)
        self._conn.request("initialize", {
            "clientInfo": _DEFAULT_CLIENT_INFO,
            "capabilities": _DEFAULT_CAPABILITIES,
        })
        self._conn.notify("initialized", {})

    def start_thread(
        self,
        *,
        sandbox: str | None = None,
        model: str | None = None,
        ephemeral: bool = True,
    ) -> str:
        """Send ``thread/start`` and return the thread id."""
        params: dict[str, Any] = {
            "cwd": self._cwd,
            "approvalPolicy": "never",
            "sandbox": sandbox or "read-only",
            "ephemeral": ephemeral,
        }
        if model is not None:
            params["model"] = model
        result = self._request("thread/start", params)
        thread = result.get("thread", {})
        return str(thread.get("id", ""))

    def resume_thread(
        self,
        thread_id: str,
        *,
        sandbox: str | None = None,
        model: str | None = None,
    ) -> str:
        """Send ``thread/resume`` and return the thread id."""
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": self._cwd,
            "approvalPolicy": "never",
            "sandbox": sandbox or "read-only",
        }
        if model is not None:
            params["model"] = model
        result = self._request("thread/resume", params)
        thread = result.get("thread", {})
        return str(thread.get("id", thread_id))

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        output_schema: dict[str, Any] | None = None,
        effort: str | None = None,
        model: str | None = None,
        timeout: float = 600,
    ) -> dict[str, Any]:
        """Send ``turn/start``, wait for ``turn/completed``, and return a result dict.

        Notifications from the app-server arrive as ``item/started`` and
        ``item/completed`` with ``params.item`` containing the item payload.
        The final assistant message is the last ``agentMessage`` item text.
        ``turn/completed`` signals the end of the turn.
        """
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema
        if effort is not None:
            params["effort"] = effort
        if model is not None:
            params["model"] = model

        conn = self._connection()

        # Send turn/start — the response is just an ack with turn metadata
        conn.request("turn/start", params, timeout=timeout)

        # Collect notifications until turn/completed
        file_changes: list[dict[str, Any]] = []
        command_executions: list[dict[str, Any]] = []
        reasoning_parts: list[str] = []
        last_agent_message: str = ""
        status: str = "completed"

        error_message: str = ""
        while True:
            # Accept any notification (not filtered by method) for this thread
            notif = conn.wait_for_notification(timeout=timeout)
            method = notif.get("method", "")
            n_params = notif.get("params", {})

            if method == "__eof__":
                raise AppServerError("app-server died during turn")

            if method == "turn/completed":
                # Only complete if this is our thread's turn
                notif_thread = n_params.get("threadId")
                if notif_thread and notif_thread != thread_id:
                    continue  # subagent turn, not ours
                turn = n_params.get("turn", {})
                status = turn.get("status", "completed")
                break

            # item/started and item/completed carry the actual content
            if method in ("item/started", "item/completed"):
                # Skip items from other threads (subagents)
                notif_thread = n_params.get("threadId")
                if notif_thread and notif_thread != thread_id:
                    continue

                item = n_params.get("item", {})
                item_type = item.get("type", "")

                if item_type == "agentMessage" and item.get("text"):
                    last_agent_message = item["text"]

                if item_type == "fileChange" and method == "item/completed":
                    file_changes.append(item)

                if item_type == "commandExecution" and method == "item/completed":
                    command_executions.append(item)

                if item_type == "reasoning" and method == "item/completed":
                    summary = item.get("summary")
                    if isinstance(summary, str) and summary.strip():
                        reasoning_parts.append(summary.strip())
                    elif isinstance(summary, list):
                        for part in summary:
                            text = part.get("text", "") if isinstance(part, dict) else str(part)
                            if text.strip():
                                reasoning_parts.append(text.strip())

            if method == "error":
                err = n_params.get("error", {})
                error_message = err.get("message", "") if isinstance(err, dict) else str(err)

            # turn/started, thread/started, etc. — just continue collecting

        return {
            "status": status,
            "thread_id": thread_id,
            "final_message": last_agent_message,
            "file_changes": file_changes,
            "command_executions": command_executions,
            "reasoning_summary": "\n".join(reasoning_parts),
            "error": error_message,
        }

    @property
    def alive(self) -> bool:
        """True if the subprocess is still running."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        """PID of the subprocess, or None."""
        return self._proc.pid if self._proc is not None else None

    def close(self) -> None:
        """Terminate the app-server process."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._proc = None

    # -- internal ----------------------------------------------------------

    def _connection(self) -> JsonRpcConnection:
        if self._conn is None:
            raise AppServerError("CodexAppServer has not been started")
        return self._conn

    def _request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 600) -> Any:
        return self._connection().request(method, params, timeout=timeout)


# ---------------------------------------------------------------------------
# ManagedServer — wraps CodexAppServer with task tracking
# ---------------------------------------------------------------------------

class ManagedServer:
    """Wraps a :class:`CodexAppServer` with task-assignment tracking."""

    def __init__(self, server: CodexAppServer) -> None:
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


# ---------------------------------------------------------------------------
# ServerManager — manages pool of ManagedServer instances
# ---------------------------------------------------------------------------

class ServerManager:
    """Thread-safe pool manager for :class:`ManagedServer` instances."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        codex_bin: str = "codex",
    ) -> None:
        self._cwd = cwd or os.getcwd()
        self._env = env
        self._codex_bin = codex_bin
        self._servers: list[ManagedServer] = []
        self._lock = threading.Lock()
        self._shutdown_done = False

        # Register cleanup handlers
        atexit.register(self.shutdown)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    # -- public API --------------------------------------------------------

    def acquire(self, task_id: str, *, resume_thread_id: str | None = None) -> ManagedServer:
        """Find an idle server or spawn a new one, assign *task_id*, and return it.

        When *resume_thread_id* is provided, prefer an idle server whose
        ``thread_history`` already contains that thread ID so the resume
        request reaches the process that created the thread.
        """
        with self._lock:
            self._reap_idle()

            # If resuming, prefer the server that owns the thread
            if resume_thread_id:
                for ms in self._servers:
                    if ms.idle and ms.alive and resume_thread_id in ms.thread_history.values():
                        ms.assign(task_id)
                        self._persist_pids()
                        return ms

            # Fall back to any idle server
            for ms in self._servers:
                if ms.idle and ms.alive:
                    ms.assign(task_id)
                    self._persist_pids()
                    return ms

            # Spawn a new one
            server = CodexAppServer(cwd=self._cwd, env=self._env, codex_bin=self._codex_bin)
            server.start()
            ms = ManagedServer(server)
            ms.assign(task_id)
            self._servers.append(ms)
            self._persist_pids()
            return ms

    def release(self, ms: ManagedServer) -> None:
        """Mark *ms* as idle."""
        with self._lock:
            ms.release()

    def shutdown(self) -> None:
        """Close all servers and clean up the PID state file."""
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            for ms in self._servers:
                try:
                    ms.server.close()
                except Exception:
                    pass
            self._servers.clear()
            state_path = os.path.join(self._cwd, SERVERS_STATE_FILENAME)
            try:
                os.remove(state_path)
            except FileNotFoundError:
                pass

    def kill_orphans(self) -> None:
        """Read ``harness-servers.json`` from a previous crash and kill listed PIDs."""
        state_path = os.path.join(self._cwd, SERVERS_STATE_FILENAME)
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        pids = data.get("pids", [])
        for pid in pids:
            try:
                pid_int = int(pid)
                # Validate process identity before killing
                if sys.platform == "linux":
                    cmdline_path = f"/proc/{pid_int}/cmdline"
                    if os.path.exists(cmdline_path):
                        with open(cmdline_path, "rb") as f:
                            cmdline = f.read().decode("utf-8", errors="replace")
                        if "codex" not in cmdline or "app-server" not in cmdline:
                            continue
                    else:
                        continue  # process gone already
                elif sys.platform == "darwin":
                    import subprocess as _sp
                    result = _sp.run(["ps", "-p", str(pid_int), "-o", "command="], capture_output=True, text=True)
                    if result.returncode != 0:
                        continue  # process gone
                    if "codex" not in result.stdout or "app-server" not in result.stdout:
                        continue
                else:
                    continue  # unknown platform, skip rather than kill blindly
                os.kill(pid_int, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, ValueError, OSError):
                pass
        try:
            os.remove(state_path)
        except FileNotFoundError:
            pass

    # -- internal ----------------------------------------------------------

    def _signal_handler(self, signum: int, frame: Any) -> None:  # noqa: ANN401
        self.shutdown()
        raise SystemExit(128 + signum)

    def _persist_pids(self) -> None:
        """Write current server PIDs to ``harness-servers.json``."""
        pids = [ms.server.pid for ms in self._servers if ms.server.pid is not None]
        state_path = os.path.join(self._cwd, SERVERS_STATE_FILENAME)
        try:
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"pids": pids}, fh)
        except OSError:
            pass

    def _reap_idle(self) -> None:
        """Close servers that have been idle for more than IDLE_TIMEOUT_SECONDS."""
        now = time.monotonic()
        remaining: list[ManagedServer] = []
        for ms in self._servers:
            if ms.idle and (now - ms.last_used) > IDLE_TIMEOUT_SECONDS:
                try:
                    ms.server.close()
                except Exception:
                    pass
            elif not ms.alive and ms.idle:
                try:
                    ms.server.close()
                except Exception:
                    pass
            else:
                remaining.append(ms)
        self._servers = remaining
