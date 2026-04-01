#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARTIFACT_VERSION = 1
EVENT_HEADER = ["seq", "timestamp", "role", "task_id", "attempt", "commit", "status", "decision", "description"]
ROLE_CHOICES = ("planner", "implementer", "verifier")
TASK_STATUS_CHOICES = ("pending", "ready", "in_progress", "blocked", "done", "failed")
RUNTIME_STATUS_CHOICES = ("idle", "running", "stopped", "terminal", "needs_human")
DEFAULT_EXECUTION_POLICY = "danger_full_access"


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    repo: Path
    launch: Path
    runtime: Path
    runtime_log: Path
    state: Path
    events: Path
    lessons: Path
    plan: Path
    tasks: Path
    reports: Path


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root(path: str | Path | None = None) -> Path:
    return Path(path or Path.cwd()).resolve()


def default_paths(repo: str | Path | None = None) -> Paths:
    base = repo_root(repo)
    return Paths(
        repo=base,
        launch=base / "harness-launch.json",
        runtime=base / "harness-runtime.json",
        runtime_log=base / "harness-runtime.log",
        state=base / "harness-state.json",
        events=base / "harness-events.tsv",
        lessons=base / "harness-lessons.md",
        plan=base / "plan.md",
        tasks=base / "tasks.json",
        reports=base / "reports",
    )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp_path = Path(handle.name)
    os.replace(tmp_path, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HarnessError(f"Missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise HarnessError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessError(f"Expected JSON object in {path}")
    return data


def build_launch_manifest(
    *,
    original_goal: str,
    prompt_text: str | None,
    config: dict[str, Any],
    approvals: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "version": ARTIFACT_VERSION,
        "mode": "harness",
        "original_goal": original_goal,
        "prompt_text": prompt_text or original_goal,
        "config": deepcopy(config),
        "approvals": deepcopy(approvals or {}),
        "defaults": deepcopy(defaults or {}),
        "notes": list(notes or []),
        "created_at": now,
        "updated_at": now,
    }


def initial_tasks_payload(goal: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "version": ARTIFACT_VERSION,
        "goal": goal,
        "planner_revision": 0,
        "tasks": [],
        "created_at": now,
        "updated_at": now,
    }


def initial_plan_text(goal: str) -> str:
    return f"# Plan\n\nGoal: {goal}\n"


def build_state_payload(*, config: dict[str, Any], run_tag: str | None = None) -> dict[str, Any]:
    return {
        "version": ARTIFACT_VERSION,
        "mode": "harness",
        "run_tag": run_tag or "",
        "config": deepcopy(config),
        "state": {
            "seq": 0,
            "planner_revision": 0,
            "active_tasks": {},
            "planner_pending_reason": "",
            "accepted_commit": "",
            "last_status": "initialized",
            "last_decision": "initialize",
            "last_verdict": "",
            "planner_runs": 0,
            "implementer_runs": 0,
            "verifier_runs": 0,
            "accepts": 0,
            "reverts": 0,
            "replans": 0,
            "blocked": 0,
            "needs_human": 0,
            "completed": False,
        },
        "updated_at": utc_now(),
    }


def normalize_state_payload(state_payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure runtime state uses the active_tasks-only execution model."""
    state = state_payload.setdefault("state", {})
    if not isinstance(state.get("active_tasks"), dict):
        state["active_tasks"] = {}
    state.setdefault("planner_pending_reason", "")
    for legacy_key in ("current_role", "current_task_id", "current_attempt", "trial_commit"):
        state.pop(legacy_key, None)
    return state_payload


def initialize_artifacts(*, paths: Paths, config: dict[str, Any], run_tag: str | None = None, force: bool = False) -> None:
    managed = (paths.state, paths.events, paths.tasks, paths.plan)
    existing = [path for path in managed if path.exists()]
    if existing and not force:
        joined = ", ".join(str(path) for path in existing)
        raise HarnessError(f"Harness artifacts already exist: {joined}")
    paths.reports.mkdir(parents=True, exist_ok=True)
    write_json_atomic(paths.state, build_state_payload(config=config, run_tag=run_tag))
    write_tasks(paths.tasks, initial_tasks_payload(str(config.get("goal", ""))))
    paths.plan.write_text(initial_plan_text(str(config.get("goal", ""))), encoding="utf-8")
    ensure_events_file(paths.events)
    append_event(
        path=paths.events,
        seq=1,
        role="runtime",
        task_id="-",
        attempt=0,
        commit="-",
        status="init",
        decision="initialize",
        description="Initialized harness state and working artifacts.",
    )
    state = read_json(paths.state)
    state["state"]["seq"] = 1
    write_json_atomic(paths.state, state)


def build_runtime_payload(*, paths: Paths, status: str, pid: int | None = None, pgid: int | None = None, command: list[str] | None = None, terminal_reason: str = "none") -> dict[str, Any]:
    now = utc_now()
    return {
        "version": ARTIFACT_VERSION,
        "repo": str(paths.repo),
        "launch_path": str(paths.launch),
        "runtime_path": str(paths.runtime),
        "log_path": str(paths.runtime_log),
        "state_path": str(paths.state),
        "events_path": str(paths.events),
        "tasks_path": str(paths.tasks),
        "status": status,
        "terminal_reason": terminal_reason,
        "pid": pid,
        "pgid": pgid,
        "command": list(command or []),
        "last_decision": "",
        "last_reason": "",
        "last_error": "",
        "last_role": "",
        "last_task_id": "",
        "created_at": now,
        "updated_at": now,
    }


def ensure_events_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(EVENT_HEADER)


def parse_events(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return [dict(row) for row in reader]


def append_event(
    *,
    path: Path,
    seq: int,
    role: str,
    task_id: str,
    attempt: int,
    commit: str,
    status: str,
    decision: str,
    description: str,
) -> dict[str, Any]:
    ensure_events_file(path)
    row = {
        "seq": str(seq),
        "timestamp": utc_now(),
        "role": role,
        "task_id": task_id or "-",
        "attempt": str(attempt),
        "commit": commit or "-",
        "status": status,
        "decision": decision,
        "description": description.strip(),
    }
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=EVENT_HEADER)
        writer.writerow(row)
    return row


def load_tasks(path: Path) -> dict[str, Any]:
    return read_json(path)


def write_tasks(path: Path, payload: dict[str, Any]) -> None:
    payload = deepcopy(payload)
    payload["updated_at"] = utc_now()
    write_json_atomic(path, payload)


def validate_task(task: dict[str, Any]) -> None:
    required = ("id", "title", "description", "acceptance_criteria", "status", "priority", "dependencies")
    missing = [field for field in required if field not in task]
    if missing:
        raise HarnessError(f"Task is missing required fields: {', '.join(missing)}")
    if task["status"] not in TASK_STATUS_CHOICES:
        raise HarnessError(f"Unsupported task status: {task['status']!r}")
    if not isinstance(task["acceptance_criteria"], list):
        raise HarnessError("Task acceptance_criteria must be a list.")
    if not isinstance(task["dependencies"], list):
        raise HarnessError("Task dependencies must be a list.")


def validate_tasks_payload(payload: dict[str, Any]) -> None:
    if payload.get("version") != ARTIFACT_VERSION:
        raise HarnessError("Unsupported tasks.json version.")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise HarnessError("tasks.json must contain a tasks list.")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise HarnessError("Each task must be an object.")
        validate_task(task)
        task_id = str(task["id"])
        if task_id in ids:
            raise HarnessError(f"Duplicate task id: {task_id}")
        ids.add(task_id)


def task_index(tasks_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    validate_tasks_payload(tasks_payload)
    return {str(task["id"]): task for task in tasks_payload["tasks"]}


def refresh_ready_tasks(tasks_payload: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(tasks_payload)
    index = task_index(payload)
    changed = False
    for task in payload["tasks"]:
        status = str(task["status"])
        if status in {"done", "failed", "blocked", "in_progress"}:
            continue
        dependencies = [str(dep) for dep in task["dependencies"]]
        if dependencies and any(index[dep]["status"] != "done" for dep in dependencies):
            if status == "ready":
                task["status"] = "pending"
                changed = True
            continue
        if status == "pending":
            task["status"] = "ready"
            changed = True
    if changed:
        payload["updated_at"] = utc_now()
    return payload


def next_ready_task(tasks_payload: dict[str, Any]) -> dict[str, Any] | None:
    payload = refresh_ready_tasks(tasks_payload)
    ready = [task for task in payload["tasks"] if task["status"] == "ready"]
    if not ready:
        return None
    ready.sort(key=lambda task: (int(task.get("priority", 100)), str(task["id"])))
    return deepcopy(ready[0])


def all_ready_tasks(tasks_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all tasks with status 'ready', sorted by priority then id."""
    payload = refresh_ready_tasks(tasks_payload)
    ready = [task for task in payload["tasks"] if task["status"] == "ready"]
    ready.sort(key=lambda task: (int(task.get("priority", 100)), str(task["id"])))
    return [deepcopy(t) for t in ready]


def all_tasks_done(tasks_payload: dict[str, Any]) -> bool:
    payload = refresh_ready_tasks(tasks_payload)
    tasks = payload["tasks"]
    return bool(tasks) and all(str(task["status"]) == "done" for task in tasks)


def report_path_for_role(paths: Paths, role: str, *, planner_revision: int = 0, task_id: str = "", attempt: int = 0) -> Path:
    paths.reports.mkdir(parents=True, exist_ok=True)
    if role == "planner":
        return paths.reports / f"planner-r{planner_revision:03d}.json"
    if role == "implementer":
        return paths.reports / f"impl-{task_id}-a{attempt}.json"
    if role == "verifier":
        return paths.reports / f"verdict-{task_id}-a{attempt}.json"
    raise HarnessError(f"Unsupported role: {role!r}")


def git_head_commit(repo: Path) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise HarnessError(completed.stderr.strip() or "Failed to determine git HEAD.")
    return completed.stdout.strip()
