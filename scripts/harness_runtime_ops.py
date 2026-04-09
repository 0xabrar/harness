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
from typing import Any

from harness_app_server import AppServerError, ServerManager
from harness_artifacts import (
    DEFAULT_EXECUTION_POLICY,
    HarnessError,
    Paths,
    all_ready_tasks,
    all_tasks_done,
    build_launch_manifest,
    build_runtime_payload,
    default_recovery_payload,
    default_paths,
    load_tasks,
    normalize_state_payload,
    read_json,
    refresh_ready_tasks,
    task_index,
    utc_now,
    write_json_atomic,
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


def sandbox_for_role(role: str, execution_policy: str = "danger_full_access") -> str:
    """Return the app-server sandbox mode for *role* under *execution_policy*."""
    policy_map = _POLICY_SANDBOX.get(execution_policy, _POLICY_SANDBOX["danger_full_access"])
    return policy_map.get(role, "read-only")


def _active_tasks(state_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return normalize_state_payload(state_payload)["state"]["active_tasks"]


def _write_state_payload(paths: Paths, state_payload: dict[str, Any]) -> None:
    state_payload["updated_at"] = utc_now()
    write_json_atomic(paths.state, state_payload)


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
                turn = run_role_turn(
                    manager=manager,
                    role="verifier",
                    task_id=task_id,
                    prompt=build_verifier_prompt(
                        paths,
                        task_id=task_id,
                        attempt=int(record.get("attempt") or 0),
                        trial_commit=str(record.get("trial_commit") or ""),
                    ),
                    repo=Path(str(record.get("worktree_path") or paths.repo)),
                    sandbox=sandbox_for_role("verifier", execution_policy),
                )
                with supervisor_lock:
                    decision = evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"])
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
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)

            if decision["decision"] == "stop":
                runtime["status"] = "terminal"
                runtime["terminal_reason"] = decision["reason"]
                _clear_runtime_recovery(runtime)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 0
            if decision["decision"] == "recovery":
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
            time.sleep(args.sleep_seconds)
            continue

        planner_pending_reason = str(state_payload["state"].get("planner_pending_reason") or "")
        if planner_pending_reason and not active:
            state_payload["state"]["planner_pending_reason"] = ""
            _write_state_payload(paths, state_payload)

            runtime["last_role"] = "planner"
            runtime["last_task_id"] = ""
            persist_runtime(paths.runtime, runtime)

            try:
                turn = run_role_turn(
                    manager=manager,
                    role="planner",
                    task_id="",
                    prompt=build_planner_prompt(paths),
                    repo=paths.repo,
                    sandbox=sandbox_for_role("planner", execution_policy),
                )
                with supervisor_lock:
                    decision = evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"])
            except (HarnessError, AppServerError, OSError) as exc:
                _set_runtime_recovery(runtime, reason=str(exc), resume_role="planner")
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2

            runtime["last_decision"] = decision["decision"]
            runtime["last_reason"] = decision["reason"]
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)

            if decision["decision"] == "stop":
                runtime["status"] = "terminal"
                runtime["terminal_reason"] = decision["reason"]
                _clear_runtime_recovery(runtime)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 0
            if decision["decision"] == "recovery":
                _set_runtime_recovery(runtime, reason=decision["reason"], owner="planner", resume_role="planner")
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2
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
                    _clear_runtime_recovery(runtime)
                    persist_runtime(paths.runtime, runtime)
                else:
                    task = batch_tasks[0]
                    task_id = str(task["id"])
                    record = _active_tasks(state_payload)[task_id]
                    workspace = prepare_task_worktree(
                        repo=paths.repo,
                        task_id=task_id,
                        branch_name=str(record.get("branch_name") or ""),
                        worktree_path=str(record.get("worktree_path") or ""),
                        base_commit=str(record.get("base_commit") or ""),
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
                    turn = run_role_turn(
                        manager=manager,
                        role="implementer",
                        task_id=task_id,
                        prompt=prompt,
                        repo=Path(workspace["worktree_path"]),
                        sandbox=sandbox_for_role("implementer", execution_policy),
                        resume_thread_id=str(record.get("thread_id") or "") or None,
                    )
                    _persist_thread_id(paths, task_id, str(turn["thread_id"]))
                    with supervisor_lock:
                        decision = evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"])
                    runtime["last_decision"] = decision["decision"]
                    runtime["last_reason"] = decision["reason"]
                    runtime["last_task_id"] = task_id
                    _clear_runtime_recovery(runtime)
                    persist_runtime(paths.runtime, runtime)
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
                _set_runtime_recovery(runtime, reason=decision["reason"], owner="planner", resume_role="planner")
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2
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
            turn = run_role_turn(
                manager=manager,
                role="planner",
                task_id="",
                prompt=build_planner_prompt(paths),
                repo=paths.repo,
                sandbox=sandbox_for_role("planner", execution_policy),
            )
            with supervisor_lock:
                decision = evaluate_supervisor_status(repo=paths.repo, report_override=turn["report"])
        except (HarnessError, AppServerError, OSError) as exc:
            _set_runtime_recovery(runtime, reason=str(exc), resume_role="planner")
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 2

        runtime["last_decision"] = decision["decision"]
        runtime["last_reason"] = decision["reason"]
        _clear_runtime_recovery(runtime)
        persist_runtime(paths.runtime, runtime)

        if decision["decision"] == "stop":
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = decision["reason"]
            _clear_runtime_recovery(runtime)
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0
        if decision["decision"] == "recovery":
            _set_runtime_recovery(runtime, reason=decision["reason"], owner="planner", resume_role="planner")
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 2
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
