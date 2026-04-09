#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from harness_app_server import AppServerError, ServerManager
from harness_artifacts import (
    DEFAULT_EXECUTION_POLICY,
    HarnessError,
    Paths,
    all_ready_tasks,
    all_tasks_done,
    build_state_payload,
    build_launch_manifest,
    build_runtime_payload,
    default_recovery_payload,
    default_paths,
    ensure_events_file,
    load_tasks,
    normalize_recovery_payload,
    normalize_state_payload,
    parse_events,
    read_json,
    refresh_ready_tasks,
    task_index,
    utc_now,
    write_json_atomic,
    write_tasks,
)
from harness_build_prompt import (
    build_implementer_prompt_for_task,
    build_planner_prompt,
    build_verifier_prompt,
)
from harness_init_run import initialize_run
from harness_launch_gate import evaluate_launch_context
from harness_report_parser import parse_structured_output
from harness_runtime_common import ensure_runtime_not_running, persist_runtime
from harness_schemas import load_schema
from harness_supervisor_status import evaluate_supervisor_status
from harness_task_worktree import prepare_task_worktree


_POLICY_SANDBOX: dict[str, dict[str, str]] = {
    "danger_full_access": {
        "planner": "danger-full-access",
        "implementer": "danger-full-access",
        "verifier": "danger-full-access",
    },
    "workspace_write": {
        "planner": "workspace-write",
        "implementer": "workspace-write",
        "verifier": "read-only",
    },
}

_RUNTIME_RETRY_LIMIT = 1


class RuntimeRetryExhausted(HarnessError):
    """Raised when a retryable runtime fault exceeds the in-process retry budget."""

    def __init__(
        self,
        *,
        reason: str,
        resume_role: str,
        resume_task_id: str,
        resume_attempt: int,
        cause: BaseException,
    ) -> None:
        super().__init__(str(cause))
        self.reason = reason
        self.resume_role = resume_role
        self.resume_task_id = resume_task_id
        self.resume_attempt = resume_attempt
        self.cause = cause


def sandbox_for_role(role: str, execution_policy: str = "danger_full_access") -> str:
    """Return the app-server sandbox mode for *role* under *execution_policy*."""
    policy_map = _POLICY_SANDBOX.get(execution_policy, _POLICY_SANDBOX["danger_full_access"])
    return policy_map.get(role, "read-only")


def _active_tasks(state_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return normalize_state_payload(state_payload)["state"]["active_tasks"]


def _write_state_payload(paths: Paths, state_payload: dict[str, Any]) -> None:
    state_payload["updated_at"] = utc_now()
    write_json_atomic(paths.state, state_payload)


def _bootstrap_missing_run_artifacts(paths: Paths, launch: dict[str, Any]) -> list[str]:
    created: list[str] = []
    config = dict(launch.get("config") or {})

    if not paths.events.exists():
        ensure_events_file(paths.events)
        created.append("harness-events.tsv")

    if not paths.state.exists():
        state_payload = build_state_payload(config=config, run_tag="")
        existing_events = parse_events(paths.events)
        if existing_events:
            try:
                state_payload["state"]["seq"] = max(int(row.get("seq") or 0) for row in existing_events)
            except ValueError:
                state_payload["state"]["seq"] = len(existing_events)
        write_json_atomic(paths.state, state_payload)
        created.append("harness-state.json")

    return created


def _task_sort_key(task: dict[str, Any]) -> tuple[int, str]:
    return (int(task.get("priority", 100)), str(task["id"]))


def _sorted_active_task_ids(tasks_payload: dict[str, Any], state_payload: dict[str, Any], *, role: str) -> list[str]:
    active = _active_tasks(state_payload)
    index = task_index(tasks_payload)
    ids = [task_id for task_id, record in active.items() if str(record.get("role") or "") == role]
    ids.sort(key=lambda task_id: _task_sort_key(index.get(task_id, {"id": task_id, "priority": 100})))
    return ids


def _ready_tasks_not_active(tasks_payload: dict[str, Any], state_payload: dict[str, Any]) -> list[dict[str, Any]]:
    active = _active_tasks(state_payload)
    return [task for task in all_ready_tasks(tasks_payload) if str(task["id"]) not in active]


def _has_unfinished_tasks(tasks_payload: dict[str, Any]) -> bool:
    return any(str(task.get("status") or "") != "done" for task in tasks_payload.get("tasks", []))


def _clear_runtime_recovery(runtime: dict[str, Any]) -> None:
    runtime["recovery"] = default_recovery_payload()
    runtime["last_error"] = ""


def _set_runtime_recovery(
    runtime: dict[str, Any],
    *,
    reason: str,
    owner: str = "runtime",
    resume_role: str = "",
    resume_task_id: str = "",
    resume_attempt: int = 0,
) -> None:
    recovery = default_recovery_payload()
    recovery.update(
        {
            "status": "pending",
            "owner": owner,
            "reason": reason,
            "resume_role": resume_role,
            "resume_task_id": resume_task_id,
            "resume_attempt": resume_attempt,
        }
    )
    runtime["status"] = "recovery"
    runtime["terminal_reason"] = reason
    runtime["last_error"] = reason
    runtime["recovery"] = recovery


def _record_runtime_retry(
    paths: Paths,
    runtime: dict[str, Any],
    *,
    reason: str,
    error_message: str,
    resume_role: str,
    resume_task_id: str,
    resume_attempt: int,
    exhausted: bool = False,
    retry_count_override: int | None = None,
) -> int:
    state_payload = normalize_state_payload(read_json(paths.state))
    existing = normalize_recovery_payload(state_payload["state"].get("recovery"))
    same_retry = (
        str(existing.get("owner") or "") == "runtime"
        and str(existing["retry"].get("reason") or "") == reason
        and str(existing["retry"].get("resume_role") or "") == resume_role
        and str(existing["retry"].get("resume_task_id") or "") == resume_task_id
        and int(existing["retry"].get("resume_attempt") or 0) == int(resume_attempt or 0)
    )
    retry_count = retry_count_override
    if retry_count is None:
        retry_count = (int(existing["retry"].get("count") or 0) if same_retry else 0) + 1

    recovery = default_recovery_payload()
    recovery.update(
        {
            "status": "pending",
            "owner": "runtime",
            "reason": reason,
            "resume_role": resume_role,
            "resume_task_id": resume_task_id,
            "resume_attempt": resume_attempt,
        }
    )
    recovery["retry"].update(
        {
            "count": retry_count,
            "reason": reason,
            "resume_role": resume_role,
            "resume_task_id": resume_task_id,
            "resume_attempt": resume_attempt,
        }
    )

    state_payload["state"]["last_status"] = "recovery"
    state_payload["state"]["recovery"] = recovery
    _write_state_payload(paths, state_payload)

    runtime["status"] = "recovery" if exhausted else "running"
    runtime["terminal_reason"] = error_message if exhausted else "none"
    runtime["last_decision"] = "recovery" if exhausted else "runtime_retry"
    runtime["last_reason"] = reason
    runtime["last_error"] = error_message
    runtime["recovery"] = recovery
    persist_runtime(paths.runtime, runtime)
    return retry_count


def _clear_runtime_retry_state(paths: Paths, runtime: dict[str, Any]) -> None:
    state_payload = normalize_state_payload(read_json(paths.state))
    if str(state_payload["state"]["recovery"].get("owner") or "") == "runtime":
        state_payload["state"]["recovery"] = default_recovery_payload()
        _write_state_payload(paths, state_payload)
    if str(runtime.get("recovery", {}).get("owner") or "") == "runtime":
        runtime["status"] = "running"
        runtime["terminal_reason"] = "none"
        _clear_runtime_recovery(runtime)
        persist_runtime(paths.runtime, runtime)


def _schedule_planner_follow_up(
    paths: Paths,
    runtime: dict[str, Any],
    *,
    reason: str,
    task_id: str = "",
    task_reason: str = "",
) -> None:
    state_payload = normalize_state_payload(read_json(paths.state))
    active = _active_tasks(state_payload)
    if task_id:
        active.pop(task_id, None)

    tasks_payload = load_tasks(paths.tasks)
    tasks_changed = False
    if task_id:
        task = task_index(tasks_payload).get(task_id)
        if task is not None and str(task.get("status") or "") != "done":
            if str(task.get("status") or "") != "failed":
                task["status"] = "blocked"
            task["blocked_reason"] = task_reason or reason
            tasks_changed = True

    if not str(state_payload["state"].get("planner_pending_reason") or ""):
        state_payload["state"]["planner_pending_reason"] = reason or "planner_recovery_pending"
    state_payload["state"]["last_status"] = "recovery"
    _write_state_payload(paths, state_payload)
    if tasks_changed:
        write_tasks(paths.tasks, tasks_payload)

    runtime["status"] = "running"
    runtime["terminal_reason"] = "none"
    runtime["recovery"] = state_payload["state"]["recovery"]
    persist_runtime(paths.runtime, runtime)


def _continue_planner_recovery(paths: Paths, runtime: dict[str, Any], *, reason: str) -> bool:
    if not _has_unfinished_tasks(load_tasks(paths.tasks)):
        return False
    _schedule_planner_follow_up(paths, runtime, reason=reason)
    return True


def _handoff_runtime_retry_to_planner(paths: Paths, runtime: dict[str, Any], exc: RuntimeRetryExhausted) -> None:
    _schedule_planner_follow_up(
        paths,
        runtime,
        reason=exc.reason,
        task_id=exc.resume_task_id,
        task_reason=str(exc.cause) or exc.reason,
    )


def _is_retryable_app_server_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        return True
    if isinstance(exc, AppServerError):
        message = str(exc).lower()
        return any(token in message for token in ("connection", "closed", "timeout"))
    if isinstance(exc, HarnessError):
        return "app-server connection failed" in str(exc).lower()
    return False


def _run_with_runtime_retries(
    *,
    paths: Paths,
    runtime: dict[str, Any],
    reason: str,
    resume_role: str,
    resume_task_id: str,
    resume_attempt: int,
    sleep_seconds: float,
    is_retryable: Callable[[BaseException], bool],
    operation: Callable[[], Any],
) -> Any:
    while True:
        try:
            result = operation()
        except Exception as exc:
            if not is_retryable(exc):
                raise
            retry_count = _record_runtime_retry(
                paths,
                runtime,
                reason=reason,
                error_message=str(exc),
                resume_role=resume_role,
                resume_task_id=resume_task_id,
                resume_attempt=resume_attempt,
                exhausted=False,
            )
            if retry_count > _RUNTIME_RETRY_LIMIT:
                _record_runtime_retry(
                    paths,
                    runtime,
                    reason=reason,
                    error_message=str(exc),
                    resume_role=resume_role,
                    resume_task_id=resume_task_id,
                    resume_attempt=resume_attempt,
                    exhausted=True,
                    retry_count_override=retry_count,
                )
                raise RuntimeRetryExhausted(
                    reason=reason,
                    resume_role=resume_role,
                    resume_task_id=resume_task_id,
                    resume_attempt=resume_attempt,
                    cause=exc,
                ) from exc
            time.sleep(sleep_seconds)
            continue
        _clear_runtime_retry_state(paths, runtime)
        return result


def _run_parallel_implementers(
    *,
    manager: ServerManager,
    ready_tasks: list[dict],
    paths: Paths,
    runtime: dict,
    execution_policy: str = "danger_full_access",
    task_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Run implementers for multiple independent tasks concurrently."""
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def _run_one(task: dict) -> None:
        task_id = str(task["id"])
        try:
            record = (task_states or {}).get(task_id, {})
            workspace = prepare_task_worktree(
                repo=paths.repo,
                task_id=task_id,
                branch_name=str(record.get("branch_name") or ""),
                worktree_path=str(record.get("worktree_path") or ""),
                base_commit=str(record.get("base_commit") or ""),
            )
            attempt = int(record.get("attempt") or (int(task.get("attempts", 0)) + 1))
            prompt = build_implementer_prompt_for_task(paths, task, attempt=attempt)
            feedback = str(record.get("verifier_feedback") or "")
            if feedback:
                prompt = (
                    "[VERIFIER FEEDBACK FROM PREVIOUS ATTEMPT]\n"
                    f"{feedback}\n"
                    "[END VERIFIER FEEDBACK]\n\n"
                    f"{prompt}"
                )
            turn = run_role_turn(
                manager=manager,
                role="implementer",
                task_id=task_id,
                prompt=prompt,
                repo=Path(workspace["worktree_path"]),
                sandbox=sandbox_for_role("implementer", execution_policy),
                resume_thread_id=str(record.get("thread_id") or "") or None,
            )
            turn["workspace"] = workspace
            results[task_id] = turn
        except Exception as exc:
            errors[task_id] = str(exc)

    threads = []
    for task in ready_tasks:
        thread = threading.Thread(target=_run_one, args=(task,))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()

    return results, errors


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
    """Execute one agent turn for *role* via the app-server protocol."""
    key = task_id or role
    managed = manager.acquire(key, resume_thread_id=resume_thread_id, cwd=str(repo))
    retried = False
    try:
        while True:
            try:
                if resume_thread_id:
                    thread_id = managed.server.resume_thread(resume_thread_id, sandbox=sandbox)
                else:
                    thread_id = managed.server.start_thread(sandbox=sandbox)

                schema = load_schema(role)
                result = managed.server.run_turn(thread_id, prompt, output_schema=schema)
                if result.get("status") != "completed":
                    error_msg = result.get("error") or f"Turn ended with status: {result.get('status')}"
                    raise HarnessError(f"{role} turn did not complete: {error_msg}")

                managed.thread_history[key] = thread_id
                parsed = parse_structured_output(result.get("final_message"))
                if parsed.get("parsed") is None:
                    raise HarnessError(
                        f"Failed to parse {role} report: {parsed.get('parse_error', 'unknown error')}"
                    )

                return {
                    "report": parsed["parsed"],
                    "thread_id": thread_id,
                    "turn_result": result,
                    "parse_error": parsed.get("parse_error"),
                }
            except (BrokenPipeError, ConnectionError, EOFError):
                if retried:
                    raise HarnessError(f"App-server connection failed twice for role {role!r}")
                retried = True
                resume_thread_id = None
                try:
                    managed.server.close()
                except Exception:
                    pass
                manager.release(managed)
                managed = manager.acquire(key, cwd=str(repo))
            except AppServerError:
                if managed.alive:
                    raise
                if retried:
                    raise HarnessError(f"App-server connection failed twice for role {role!r}")
                retried = True
                resume_thread_id = None
                try:
                    managed.server.close()
                except Exception:
                    pass
                manager.release(managed)
                managed = manager.acquire(key, cwd=str(repo))
    finally:
        manager.release(managed)


def command_is_executable(command: str) -> bool:
    return any((Path(candidate) / command).exists() for candidate in os.environ.get("PATH", "").split(os.pathsep))


def create_launch_manifest(args: argparse.Namespace) -> dict[str, Any]:
    paths = default_paths(args.repo)
    manifest = build_launch_manifest(
        original_goal=args.original_goal,
        prompt_text=args.prompt_text,
        config={
            "goal": args.goal,
            "scope": args.scope,
            "session_mode": "background",
            "execution_policy": args.execution_policy,
            "stop_condition": args.stop_condition or "",
            "allow_task_expansion": args.allow_task_expansion,
            "max_task_attempts": args.max_task_attempts,
        },
        approvals={},
        defaults={},
        notes=args.note or [],
    )
    write_json_atomic(paths.launch, manifest)
    return {"launch_path": str(paths.launch), "goal": args.goal, "mode": "harness"}


def start_runtime(args: argparse.Namespace, *, runner_path: Path) -> dict[str, Any]:
    paths = default_paths(args.repo)
    ensure_runtime_not_running(paths)
    if not command_is_executable(args.codex_bin):
        raise HarnessError(f"Codex executable is not available: {args.codex_bin}")
    gate = evaluate_launch_context(repo=paths.repo)
    if gate["decision"] not in {"fresh", "resumable"}:
        raise HarnessError(f"Cannot start runtime: {gate['reason']}")
    if not paths.launch.exists():
        raise HarnessError(f"Missing JSON file: {paths.launch}")
    launch = read_json(paths.launch)
    if gate["decision"] == "fresh" and not paths.state.exists():
        initialize_run(
            repo=paths.repo,
            goal=str(launch.get("config", {}).get("goal") or launch.get("original_goal") or ""),
            scope=str(launch.get("config", {}).get("scope") or "."),
            session_mode="background",
            execution_policy=str(launch.get("config", {}).get("execution_policy") or DEFAULT_EXECUTION_POLICY),
            stop_condition=str(launch.get("config", {}).get("stop_condition") or ""),
            allow_task_expansion=str(launch.get("config", {}).get("allow_task_expansion") or "enabled"),
            max_task_attempts=int(launch.get("config", {}).get("max_task_attempts") or 3),
            force=False,
        )
    _bootstrap_missing_run_artifacts(paths, launch)

    command = [
        sys.executable,
        str(runner_path),
        "run",
        "--repo",
        str(paths.repo),
        "--codex-bin",
        args.codex_bin,
    ]
    paths.runtime_log.parent.mkdir(parents=True, exist_ok=True)
    log_handle = paths.runtime_log.open("a", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=paths.repo,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log_handle.close()
    runtime = build_runtime_payload(
        paths=paths,
        status="running",
        pid=process.pid,
        pgid=os.getpgid(process.pid),
        command=command,
    )
    persist_runtime(paths.runtime, runtime)
    return {"status": "running", "pid": process.pid, "runtime_path": str(paths.runtime), "log_path": str(paths.runtime_log)}


def launch_and_start_runtime(args: argparse.Namespace, *, runner_path: Path) -> dict[str, Any]:
    created = create_launch_manifest(args)
    initialize_run(
        repo=args.repo,
        goal=args.goal,
        scope=args.scope,
        session_mode="background",
        execution_policy=args.execution_policy,
        run_tag="",
        stop_condition=args.stop_condition or "",
        allow_task_expansion=args.allow_task_expansion,
        max_task_attempts=args.max_task_attempts,
        force=args.force if hasattr(args, "force") else False,
    )
    started = start_runtime(args, runner_path=runner_path)
    return {**created, **started}


def _persist_thread_id(paths: Paths, task_id: str, thread_id: str) -> None:
    state_payload = normalize_state_payload(read_json(paths.state))
    active = _active_tasks(state_payload)
    if task_id in active:
        active[task_id]["thread_id"] = thread_id
        _write_state_payload(paths, state_payload)


def run_runtime(args: argparse.Namespace) -> int:
    paths = default_paths(args.repo)
    runtime = read_json(paths.runtime) if paths.runtime.exists() else build_runtime_payload(paths=paths, status="running")
    runtime["status"] = "running"
    runtime["terminal_reason"] = "none"
    persist_runtime(paths.runtime, runtime)
    launch = read_json(paths.launch)
    execution_policy = str(launch.get("config", {}).get("execution_policy") or DEFAULT_EXECUTION_POLICY)
    _bootstrap_missing_run_artifacts(paths, launch)

    codex_bin = getattr(args, "codex_bin", "codex")
    manager = ServerManager(cwd=str(paths.repo), state_dir=str(paths.repo), codex_bin=codex_bin)
    manager.kill_orphans()
    supervisor_lock = threading.Lock()

    while True:
        gate = evaluate_launch_context(repo=paths.repo, ignore_running_runtime=True)
        if gate["decision"] not in {"fresh", "resumable"}:
            _set_runtime_recovery(runtime, reason=gate["reason"], resume_role="runtime")
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 2
        if gate["decision"] == "fresh" and not paths.state.exists():
            initialize_run(
                repo=paths.repo,
                goal=str(launch.get("config", {}).get("goal") or launch.get("original_goal") or ""),
                scope=str(launch.get("config", {}).get("scope") or "."),
                session_mode="background",
                execution_policy=execution_policy,
                stop_condition=str(launch.get("config", {}).get("stop_condition") or ""),
                allow_task_expansion=str(launch.get("config", {}).get("allow_task_expansion") or "enabled"),
                max_task_attempts=int(launch.get("config", {}).get("max_task_attempts") or 3),
                force=False,
            )
        _bootstrap_missing_run_artifacts(paths, launch)

        state_payload = normalize_state_payload(read_json(paths.state))
        tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
        active = _active_tasks(state_payload)

        if all_tasks_done(tasks_payload):
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = "all_tasks_done"
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0

        verifier_ids = _sorted_active_task_ids(tasks_payload, state_payload, role="verifier")
        if verifier_ids:
            task_id = verifier_ids[0]
            record = active[task_id]

            runtime["last_role"] = "verifier"
            runtime["last_task_id"] = task_id
            persist_runtime(paths.runtime, runtime)

            try:
                attempt = int(record.get("attempt") or 0)
                turn = _run_with_runtime_retries(
                    paths=paths,
                    runtime=runtime,
                    reason="app_server_turn_failed",
                    resume_role="verifier",
                    resume_task_id=task_id,
                    resume_attempt=attempt,
                    sleep_seconds=args.sleep_seconds,
                    is_retryable=_is_retryable_app_server_error,
                    operation=lambda: run_role_turn(
                        manager=manager,
                        role="verifier",
                        task_id=task_id,
                        prompt=build_verifier_prompt(
                            paths,
                            task_id=task_id,
                            attempt=attempt,
                            trial_commit=str(record.get("trial_commit") or ""),
                        ),
                        repo=Path(str(record.get("worktree_path") or paths.repo)),
                        sandbox=sandbox_for_role("verifier", execution_policy),
                    ),
                )
                with supervisor_lock:
                    decision = _run_with_runtime_retries(
                        paths=paths,
                        runtime=runtime,
                        reason="git_or_worktree_apply_failed",
                        resume_role="verifier",
                        resume_task_id=task_id,
                        resume_attempt=attempt,
                        sleep_seconds=args.sleep_seconds,
                        is_retryable=lambda exc: isinstance(exc, (HarnessError, OSError)),
                        operation=lambda: evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"]),
                    )
            except RuntimeRetryExhausted as exc:
                runtime["last_decision"] = "run_planner"
                runtime["last_reason"] = exc.reason
                _handoff_runtime_retry_to_planner(paths, runtime, exc)
                time.sleep(args.sleep_seconds)
                continue
            except (HarnessError, AppServerError, OSError) as exc:
                _set_runtime_recovery(
                    runtime,
                    reason=str(exc),
                    resume_role="verifier",
                    resume_task_id=task_id,
                    resume_attempt=int(record.get("attempt") or 0),
                )
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2

            runtime["last_decision"] = decision["decision"]
            runtime["last_reason"] = decision["reason"]

            if decision["decision"] == "stop":
                runtime["status"] = "terminal"
                runtime["terminal_reason"] = decision["reason"]
                _clear_runtime_recovery(runtime)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 0
            if decision["decision"] == "recovery":
                if _continue_planner_recovery(paths, runtime, reason=str(decision["reason"])):
                    time.sleep(args.sleep_seconds)
                    continue
                _set_runtime_recovery(
                    runtime,
                    reason=decision["reason"],
                    owner="planner",
                    resume_role="planner",
                    resume_task_id=task_id,
                )
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)
            time.sleep(args.sleep_seconds)
            continue

        planner_pending_reason = str(state_payload["state"].get("planner_pending_reason") or "")
        if planner_pending_reason and not active:
            runtime["last_role"] = "planner"
            runtime["last_task_id"] = ""
            persist_runtime(paths.runtime, runtime)

            try:
                turn = _run_with_runtime_retries(
                    paths=paths,
                    runtime=runtime,
                    reason="app_server_turn_failed",
                    resume_role="planner",
                    resume_task_id="",
                    resume_attempt=0,
                    sleep_seconds=args.sleep_seconds,
                    is_retryable=_is_retryable_app_server_error,
                    operation=lambda: run_role_turn(
                        manager=manager,
                        role="planner",
                        task_id="",
                        prompt=build_planner_prompt(paths),
                        repo=paths.repo,
                        sandbox=sandbox_for_role("planner", execution_policy),
                    ),
                )
                with supervisor_lock:
                    decision = evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"])
            except RuntimeRetryExhausted as exc:
                runtime["last_decision"] = "run_planner"
                runtime["last_reason"] = exc.reason
                _handoff_runtime_retry_to_planner(paths, runtime, exc)
                time.sleep(args.sleep_seconds)
                continue
            except (HarnessError, AppServerError, OSError) as exc:
                _set_runtime_recovery(runtime, reason=str(exc), resume_role="planner")
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2

            runtime["last_decision"] = decision["decision"]
            runtime["last_reason"] = decision["reason"]

            if decision["decision"] == "stop":
                runtime["status"] = "terminal"
                runtime["terminal_reason"] = decision["reason"]
                _clear_runtime_recovery(runtime)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 0
            if decision["decision"] == "recovery":
                if _continue_planner_recovery(paths, runtime, reason=str(decision["reason"])):
                    time.sleep(args.sleep_seconds)
                    continue
                _set_runtime_recovery(runtime, reason=decision["reason"], owner="planner", resume_role="planner")
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)
            time.sleep(args.sleep_seconds)
            continue

        ready_tasks = _ready_tasks_not_active(tasks_payload, state_payload)
        if ready_tasks:
            for task in ready_tasks:
                active[str(task["id"])] = {
                    "role": "implementer",
                    "attempt": int(task.get("attempts", 0)) + 1,
                    "trial_commit": "",
                    "thread_id": str(active.get(str(task["id"]), {}).get("thread_id") or ""),
                    "verifier_feedback": str(active.get(str(task["id"]), {}).get("verifier_feedback") or ""),
                    "branch_name": str(active.get(str(task["id"]), {}).get("branch_name") or ""),
                    "worktree_path": str(active.get(str(task["id"]), {}).get("worktree_path") or ""),
                    "base_commit": str(active.get(str(task["id"]), {}).get("base_commit") or ""),
                }
            _write_state_payload(paths, state_payload)

        state_payload = normalize_state_payload(read_json(paths.state))
        tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
        implementer_ids = _sorted_active_task_ids(tasks_payload, state_payload, role="implementer")
        if implementer_ids:
            batch_tasks = [task_index(tasks_payload)[task_id] for task_id in implementer_ids]
            runtime["last_role"] = "implementer"
            runtime["last_task_id"] = ",".join(implementer_ids)
            persist_runtime(paths.runtime, runtime)

            try:
                if len(batch_tasks) > 1:
                    results, errors = _run_parallel_implementers(
                        manager=manager,
                        ready_tasks=batch_tasks,
                        paths=paths,
                        runtime=runtime,
                        execution_policy=execution_policy,
                        task_states=_active_tasks(state_payload),
                    )
                    for task in batch_tasks:
                        task_id = str(task["id"])
                        if task_id in errors:
                            record = _active_tasks(state_payload).get(task_id, {})
                            _set_runtime_recovery(
                                runtime,
                                reason=errors[task_id],
                                resume_role="implementer",
                                resume_task_id=task_id,
                                resume_attempt=int(record.get("attempt") or 0),
                            )
                            persist_runtime(paths.runtime, runtime)
                            manager.shutdown()
                            return 2
                        if task_id not in results:
                            continue
                        workspace = results[task_id].get("workspace", {})
                        if workspace:
                            state_payload = normalize_state_payload(read_json(paths.state))
                            active = _active_tasks(state_payload)
                            if task_id in active:
                                active[task_id].update({
                                    "branch_name": str(workspace.get("branch_name") or ""),
                                    "worktree_path": str(workspace.get("worktree_path") or ""),
                                    "base_commit": str(workspace.get("base_commit") or ""),
                                })
                                _write_state_payload(paths, state_payload)
                        _persist_thread_id(paths, task_id, str(results[task_id]["thread_id"]))
                        with supervisor_lock:
                            decision = evaluate_supervisor_status(repo=paths.repo, report_override=results[task_id]["report"])
                    runtime["last_decision"] = decision["decision"]
                    runtime["last_reason"] = decision["reason"]
                else:
                    task = batch_tasks[0]
                    task_id = str(task["id"])
                    record = _active_tasks(state_payload)[task_id]
                    workspace = _run_with_runtime_retries(
                        paths=paths,
                        runtime=runtime,
                        reason="worktree_prepare_failed",
                        resume_role="implementer",
                        resume_task_id=task_id,
                        resume_attempt=int(record.get("attempt") or 0),
                        sleep_seconds=args.sleep_seconds,
                        is_retryable=lambda exc: isinstance(exc, (HarnessError, OSError)),
                        operation=lambda: prepare_task_worktree(
                            repo=paths.repo,
                            task_id=task_id,
                            branch_name=str(record.get("branch_name") or ""),
                            worktree_path=str(record.get("worktree_path") or ""),
                            base_commit=str(record.get("base_commit") or ""),
                        ),
                    )
                    state_payload = normalize_state_payload(read_json(paths.state))
                    active = _active_tasks(state_payload)
                    if task_id in active:
                        active[task_id].update({
                            "branch_name": str(workspace["branch_name"]),
                            "worktree_path": str(workspace["worktree_path"]),
                            "base_commit": str(workspace["base_commit"]),
                        })
                        _write_state_payload(paths, state_payload)
                    prompt = build_implementer_prompt_for_task(
                        paths,
                        task,
                        attempt=int(record.get("attempt") or (int(task.get("attempts", 0)) + 1)),
                    )
                    feedback = str(record.get("verifier_feedback") or "")
                    if feedback:
                        prompt = (
                            "[VERIFIER FEEDBACK FROM PREVIOUS ATTEMPT]\n"
                            f"{feedback}\n"
                            "[END VERIFIER FEEDBACK]\n\n"
                            f"{prompt}"
                        )
                    turn = _run_with_runtime_retries(
                        paths=paths,
                        runtime=runtime,
                        reason="app_server_turn_failed",
                        resume_role="implementer",
                        resume_task_id=task_id,
                        resume_attempt=int(record.get("attempt") or 0),
                        sleep_seconds=args.sleep_seconds,
                        is_retryable=_is_retryable_app_server_error,
                        operation=lambda: run_role_turn(
                            manager=manager,
                            role="implementer",
                            task_id=task_id,
                            prompt=prompt,
                            repo=Path(workspace["worktree_path"]),
                            sandbox=sandbox_for_role("implementer", execution_policy),
                            resume_thread_id=str(record.get("thread_id") or "") or None,
                        ),
                    )
                    _persist_thread_id(paths, task_id, str(turn["thread_id"]))
                    with supervisor_lock:
                        decision = evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"])
                    runtime["last_decision"] = decision["decision"]
                    runtime["last_reason"] = decision["reason"]
                    runtime["last_task_id"] = task_id
            except RuntimeRetryExhausted as exc:
                runtime["last_decision"] = "run_planner"
                runtime["last_reason"] = exc.reason
                _handoff_runtime_retry_to_planner(paths, runtime, exc)
                time.sleep(args.sleep_seconds)
                continue
            except (HarnessError, AppServerError, OSError) as exc:
                active = _active_tasks(state_payload)
                task_id = implementer_ids[0] if implementer_ids else ""
                record = active.get(task_id, {})
                _set_runtime_recovery(
                    runtime,
                    reason=str(exc),
                    resume_role="implementer",
                    resume_task_id=task_id,
                    resume_attempt=int(record.get("attempt") or 0),
                )
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2

            if decision["decision"] == "stop":
                runtime["status"] = "terminal"
                runtime["terminal_reason"] = decision["reason"]
                _clear_runtime_recovery(runtime)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 0
            if decision["decision"] == "recovery":
                if _continue_planner_recovery(paths, runtime, reason=str(decision["reason"])):
                    time.sleep(args.sleep_seconds)
                    continue
                _set_runtime_recovery(runtime, reason=decision["reason"], owner="planner", resume_role="planner")
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)
            time.sleep(args.sleep_seconds)
            continue

        state_payload = normalize_state_payload(read_json(paths.state))
        tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
        if all_tasks_done(tasks_payload):
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = "all_tasks_done"
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0

        runtime["last_role"] = "planner"
        runtime["last_task_id"] = ""
        persist_runtime(paths.runtime, runtime)

        try:
            turn = _run_with_runtime_retries(
                paths=paths,
                runtime=runtime,
                reason="app_server_turn_failed",
                resume_role="planner",
                resume_task_id="",
                resume_attempt=0,
                sleep_seconds=args.sleep_seconds,
                is_retryable=_is_retryable_app_server_error,
                operation=lambda: run_role_turn(
                    manager=manager,
                    role="planner",
                    task_id="",
                    prompt=build_planner_prompt(paths),
                    repo=paths.repo,
                    sandbox=sandbox_for_role("planner", execution_policy),
                ),
            )
            with supervisor_lock:
                decision = evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"])
        except RuntimeRetryExhausted as exc:
            runtime["last_decision"] = "run_planner"
            runtime["last_reason"] = exc.reason
            _handoff_runtime_retry_to_planner(paths, runtime, exc)
            time.sleep(args.sleep_seconds)
            continue
        except (HarnessError, AppServerError, OSError) as exc:
            _set_runtime_recovery(runtime, reason=str(exc), resume_role="planner")
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 2

        runtime["last_decision"] = decision["decision"]
        runtime["last_reason"] = decision["reason"]

        if decision["decision"] == "stop":
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = decision["reason"]
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0
        if decision["decision"] == "recovery":
            if _continue_planner_recovery(paths, runtime, reason=str(decision["reason"])):
                time.sleep(args.sleep_seconds)
                continue
            _set_runtime_recovery(runtime, reason=decision["reason"], owner="planner", resume_role="planner")
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 2
        _clear_runtime_recovery(runtime)
        persist_runtime(paths.runtime, runtime)
        time.sleep(args.sleep_seconds)


def stop_runtime(args: argparse.Namespace) -> dict[str, Any]:
    paths = default_paths(args.repo)
    runtime = read_json(paths.runtime)
    pid = runtime.get("pid")
    pgid = runtime.get("pgid") or pid
    if pid and pgid:
        try:
            os.killpg(int(pgid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    runtime["status"] = "terminal"
    runtime["terminal_reason"] = "user_stopped"
    _clear_runtime_recovery(runtime)
    persist_runtime(paths.runtime, runtime)
    return {"status": "terminal", "runtime_path": str(paths.runtime), "pid": pid, "pgid": pgid}
