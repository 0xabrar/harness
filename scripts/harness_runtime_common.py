#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harness_artifacts import (
    DEFAULT_EXECUTION_POLICY,
    EXECUTION_POLICY_CHOICES,
    HarnessError,
    Paths,
    default_paths,
    read_json,
    utc_now,
    write_json_atomic,
)


def load_runtime(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return read_json(path)


def pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    return True


def persist_runtime(path: Path, payload: dict[str, Any]) -> None:
    updated = dict(payload)
    updated["updated_at"] = utc_now()
    write_json_atomic(path, updated)


def ensure_runtime_not_running(paths: Paths) -> None:
    runtime = load_runtime(paths.runtime)
    if runtime and pid_is_alive(runtime.get("pid")):
        raise HarnessError("A harness runtime is already active for this repo.")


def codex_args_for_execution_policy(execution_policy: str | None, *, extra_args: list[str] | None = None) -> list[str]:
    policy = execution_policy or DEFAULT_EXECUTION_POLICY
    if policy not in EXECUTION_POLICY_CHOICES:
        raise HarnessError(f"Unsupported execution policy: {policy!r}")
    extras = list(extra_args or [])
    if policy == "workspace_write":
        return ["--full-auto", *extras]
    return ["--dangerously-bypass-approvals-and-sandbox", *extras]


def runtime_summary(paths: Paths) -> dict[str, Any]:
    runtime = load_runtime(paths.runtime)
    if not runtime:
        return {
            "status": "idle",
            "repo": str(paths.repo),
            "runtime_path": str(paths.runtime),
            "log_path": str(paths.runtime_log),
        }
    alive = pid_is_alive(runtime.get("pid"))
    payload = dict(runtime)
    payload["runtime_running"] = alive
    if payload.get("status") == "running" and not alive:
        payload["status"] = "needs_human"
        payload["terminal_reason"] = "runtime_process_missing"
    return payload

