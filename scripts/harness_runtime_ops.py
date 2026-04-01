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
    append_event,
    all_ready_tasks,
    all_tasks_done,
    build_launch_manifest,
    build_runtime_payload,
    default_paths,
    load_tasks,
    read_json,
    refresh_ready_tasks,
    report_path_for_role,
    task_index,
    utc_now,
    write_json_atomic,
    write_tasks,
)
from harness_init_run import initialize_run
from harness_build_prompt import build_implementer_prompt, build_implementer_prompt_for_task, build_planner_prompt, build_verifier_prompt
from harness_launch_gate import evaluate_launch_context
from harness_lessons import append_lesson
from harness_report_parser import parse_structured_output
from harness_runtime_common import ensure_runtime_not_running, persist_runtime
from harness_schemas import load_schema
from harness_supervisor_status import evaluate_supervisor_status


# ---------------------------------------------------------------------------
# Sandbox mapping: role -> app-server sandbox mode
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Parallel implementer execution
# ---------------------------------------------------------------------------

def _run_parallel_implementers(
    *,
    manager: ServerManager,
    ready_tasks: list[dict],
    paths: Paths,
    runtime: dict,
    execution_policy: str = "danger_full_access",
    task_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Run implementers for multiple independent tasks concurrently.

    Returns ``(results, errors)`` where *results* maps task-id to the
    ``run_role_turn`` return dict and *errors* maps task-id to an error
    message string.
    """
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def _run_one(task: dict) -> None:
        task_id = str(task["id"])
        try:
            record = (task_states or {}).get(task_id, {})
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
                repo=paths.repo,
                sandbox=sandbox_for_role("implementer", execution_policy),
                resume_thread_id=str(record.get("thread_id") or "") or None,
            )
            results[task_id] = turn
        except Exception as exc:
            errors[task_id] = str(exc)

    threads = []
    for task in ready_tasks:
        t = threading.Thread(target=_run_one, args=(task,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    return results, errors


# ---------------------------------------------------------------------------
# run_role_turn — single turn via the app-server protocol
# ---------------------------------------------------------------------------

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
    """Execute one agent turn for *role* via the app-server protocol.

    Returns a dict with keys: ``report``, ``thread_id``, ``turn_result``,
    ``parse_error``.
    """
    key = task_id or role
    ms = manager.acquire(key, resume_thread_id=resume_thread_id)
    retried = False
    try:
        while True:
            try:
                if resume_thread_id:
                    thread_id = ms.server.resume_thread(resume_thread_id, sandbox=sandbox)
                else:
                    thread_id = ms.server.start_thread(sandbox=sandbox)

                schema = load_schema(role)
                result = ms.server.run_turn(thread_id, prompt, output_schema=schema)

                if result.get("status") != "completed":
                    error_msg = result.get("error") or f"Turn ended with status: {result.get('status')}"
                    raise HarnessError(f"{role} turn did not complete: {error_msg}")

                ms.thread_history[key] = thread_id

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
                resume_thread_id = None  # can't resume on a fresh server
                try:
                    ms.server.close()
                except Exception:
                    pass
                manager.release(ms)
                ms = manager.acquire(key)
            except AppServerError:
                if ms.alive:
                    raise  # logical error, not a transport failure
                if retried:
                    raise HarnessError(f"App-server connection failed twice for role {role!r}")
                retried = True
                resume_thread_id = None  # can't resume on a fresh server
                try:
                    ms.server.close()
                except Exception:
                    pass
                manager.release(ms)
                ms = manager.acquire(key)
    finally:
        manager.release(ms)


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


def prompt_for_role(paths: Paths, role: str) -> str:
    if role == "planner":
        return build_planner_prompt(paths)
    if role == "implementer":
        return build_implementer_prompt(paths)
    if role == "verifier":
        return build_verifier_prompt(paths)
    raise HarnessError(f"Unsupported role: {role!r}")


def normalize_state_payload(state_payload: dict[str, Any]) -> dict[str, Any]:
    """Backfill newer runtime fields onto older state payloads."""
    state = state_payload.setdefault("state", {})
    if not isinstance(state.get("active_tasks"), dict):
        state["active_tasks"] = {}
    state.setdefault("planner_pending_reason", "")
    return state_payload


def _write_state_payload(paths: Paths, state_payload: dict[str, Any]) -> None:
    state_payload["updated_at"] = utc_now()
    write_json_atomic(paths.state, state_payload)


def _active_tasks(state_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state = normalize_state_payload(state_payload)["state"]
    return state["active_tasks"]


def _set_legacy_slot(
    state_payload: dict[str, Any],
    *,
    role: str,
    task_id: str = "",
    attempt: int = 0,
    trial_commit: str = "",
) -> None:
    state = normalize_state_payload(state_payload)["state"]
    state["current_role"] = role
    state["current_task_id"] = task_id
    state["current_attempt"] = attempt
    state["trial_commit"] = trial_commit


def _clear_legacy_slot(state_payload: dict[str, Any]) -> None:
    _set_legacy_slot(state_payload, role="", task_id="", attempt=0, trial_commit="")


def _context_string(state_payload: dict[str, Any]) -> str:
    config = state_payload.get("config", {})
    state = state_payload.get("state", {})
    return (
        f"goal={config.get('goal', '')}; "
        f"scope={config.get('scope', '')}; "
        f"role={state.get('current_role', '')}"
    )


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


def _record_event(
    paths: Paths,
    state_payload: dict[str, Any],
    *,
    role: str,
    task_id: str,
    attempt: int,
    commit: str,
    status: str,
    decision: str,
    description: str,
) -> None:
    state = normalize_state_payload(state_payload)["state"]
    seq = int(state.get("seq", 0)) + 1
    state["seq"] = seq
    append_event(
        path=paths.events,
        seq=seq,
        role=role,
        task_id=task_id or "-",
        attempt=attempt,
        commit=commit or "-",
        status=status,
        decision=decision,
        description=description,
    )


def _revert_trial_commit(repo: Path, trial_commit: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "revert", "--no-edit", trial_commit],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise HarnessError(completed.stderr.strip() or f"Failed to revert commit {trial_commit}")


def _apply_implementer_result(
    *,
    paths: Paths,
    state_payload: dict[str, Any],
    tasks_payload: dict[str, Any],
    task: dict[str, Any],
    turn: dict[str, Any],
) -> dict[str, str]:
    state = normalize_state_payload(state_payload)["state"]
    active = _active_tasks(state_payload)
    task_id = str(task["id"])
    report = turn["report"]
    attempt = int(report.get("attempt") or active.get(task_id, {}).get("attempt") or (int(task.get("attempts", 0)) + 1))
    commit = str(report.get("commit") or report.get("trial_commit") or "")
    if not commit:
        raise HarnessError(f"Implementer report for {task_id} must include a commit.")

    report_path = report_path_for_role(paths, "implementer", task_id=task_id, attempt=attempt)
    write_json_atomic(report_path, report)

    index = task_index(tasks_payload)
    current_task = index[task_id]
    current_task["status"] = "in_progress"
    current_task["attempts"] = attempt
    current_task["last_attempt_commit"] = commit

    active[task_id] = {
        "role": "verifier",
        "attempt": attempt,
        "trial_commit": commit,
        "thread_id": str(turn.get("thread_id") or active.get(task_id, {}).get("thread_id") or ""),
        "verifier_feedback": "",
    }

    state["implementer_runs"] += 1
    state["last_status"] = "submit_trial"
    state["last_decision"] = "dispatch_verifier"
    _set_legacy_slot(state_payload, role="verifier", task_id=task_id, attempt=attempt, trial_commit=commit)

    write_tasks(paths.tasks, tasks_payload)
    _record_event(
        paths,
        state_payload,
        role="implementer",
        task_id=task_id,
        attempt=attempt,
        commit=commit,
        status="submit_trial",
        decision="dispatch_verifier",
        description=str(report.get("summary") or "Implementer submitted trial commit."),
    )
    _write_state_payload(paths, state_payload)
    return {"decision": "relaunch", "reason": "dispatch_verifier"}


def _apply_verifier_result(
    *,
    paths: Paths,
    state_payload: dict[str, Any],
    tasks_payload: dict[str, Any],
    task_id: str,
    report: dict[str, Any],
) -> dict[str, str]:
    state = normalize_state_payload(state_payload)["state"]
    active = _active_tasks(state_payload)
    record = active.get(task_id)
    if record is None:
        raise HarnessError(f"Active verifier state missing for task {task_id}.")

    attempt = int(record.get("attempt") or report.get("attempt") or 0)
    commit = str(report.get("commit") or report.get("evaluated_commit") or report.get("trial_commit") or record.get("trial_commit") or "")
    if not commit:
        raise HarnessError(f"Verifier report for {task_id} must identify the evaluated commit.")

    verdict = str(report.get("verdict") or "")
    if verdict not in {"accept", "revert", "needs_human"}:
        raise HarnessError("Verifier verdict must be accept, revert, or needs_human.")

    verdict_path = report_path_for_role(paths, "verifier", task_id=task_id, attempt=attempt)
    write_json_atomic(verdict_path, report)

    index = task_index(tasks_payload)
    task = index[task_id]
    summary = str(report.get("summary") or f"Verifier returned {verdict}.")
    proposed_tasks = list(report.get("proposed_tasks") or [])
    state["verifier_runs"] += 1
    state["last_verdict"] = verdict
    state["last_status"] = verdict

    if verdict == "accept":
        task["status"] = "done"
        task["last_verdict"] = "accept"
        state["accepts"] += 1
        state["accepted_commit"] = commit
        state["trial_commit"] = ""
        active.pop(task_id, None)
        append_lesson(
            path=paths.lessons,
            title=f"Accepted task {task_id}",
            category="task",
            strategy=summary,
            outcome="accept",
            insight=summary,
            context=_context_string(state_payload),
            iteration=str(int(state.get("seq", 0)) + 1),
        )
        if proposed_tasks:
            state["planner_pending_reason"] = "planner_update_requested"

        tasks_payload = refresh_ready_tasks(tasks_payload)
        if all_tasks_done(tasks_payload):
            state["completed"] = True
            _clear_legacy_slot(state_payload)
            decision = {"decision": "stop", "reason": "all_tasks_done"}
        elif not _active_tasks(state_payload) and not _ready_tasks_not_active(tasks_payload, state_payload):
            state["planner_pending_reason"] = state.get("planner_pending_reason") or "planner_replan_needed"
            _clear_legacy_slot(state_payload)
            decision = {"decision": "relaunch", "reason": state["planner_pending_reason"]}
        else:
            _clear_legacy_slot(state_payload)
            decision = {"decision": "relaunch", "reason": "continue_after_accept"}

    elif verdict == "needs_human":
        task["status"] = "blocked"
        task["blocked_reason"] = summary
        state["needs_human"] += 1
        state["blocked"] += 1
        active.pop(task_id, None)
        _clear_legacy_slot(state_payload)
        decision = {"decision": "needs_human", "reason": "verifier_escalated"}

    else:
        _revert_trial_commit(paths.repo, commit)
        state["reverts"] += 1
        state["trial_commit"] = ""
        task["last_verdict"] = "revert"
        append_lesson(
            path=paths.lessons,
            title=f"Reverted task {task_id} attempt {attempt}",
            category="task",
            strategy=summary,
            outcome="revert",
            insight=summary,
            context=_context_string(state_payload),
            iteration=str(int(state.get("seq", 0)) + 1),
        )

        max_attempts = int(state_payload["config"].get("max_task_attempts", 3))
        if int(task.get("attempts", 0)) >= max_attempts:
            task["status"] = "failed"
            task["blocked_reason"] = summary
            active.pop(task_id, None)
            state["replans"] += 1
            state["planner_pending_reason"] = "planner_replan_after_revert"
            _clear_legacy_slot(state_payload)
            decision = {"decision": "relaunch", "reason": "planner_replan_after_revert"}
        else:
            task["status"] = "ready"
            active[task_id] = {
                "role": "implementer",
                "attempt": attempt + 1,
                "trial_commit": "",
                "thread_id": str(record.get("thread_id") or ""),
                "verifier_feedback": summary,
            }
            _set_legacy_slot(state_payload, role="implementer", task_id=task_id, attempt=attempt + 1, trial_commit="")
            decision = {"decision": "relaunch", "reason": "retry_task"}

    state["last_decision"] = decision["reason"]
    write_tasks(paths.tasks, tasks_payload)
    _record_event(
        paths,
        state_payload,
        role="verifier",
        task_id=task_id,
        attempt=attempt,
        commit=commit,
        status=verdict,
        decision=decision["reason"],
        description=summary,
    )
    _write_state_payload(paths, state_payload)
    return decision


def run_runtime(args: argparse.Namespace) -> int:
    paths = default_paths(args.repo)
    runtime = read_json(paths.runtime) if paths.runtime.exists() else build_runtime_payload(paths=paths, status="running")
    persist_runtime(paths.runtime, runtime)
    launch = read_json(paths.launch)
    execution_policy = str(launch.get("config", {}).get("execution_policy") or DEFAULT_EXECUTION_POLICY)

    codex_bin = getattr(args, "codex_bin", "codex")
    manager = ServerManager(cwd=str(paths.repo), codex_bin=codex_bin)
    manager.kill_orphans()

    supervisor_lock = threading.Lock()

    while True:
        gate = evaluate_launch_context(repo=paths.repo, ignore_running_runtime=True)
        if gate["decision"] not in {"fresh", "resumable"}:
            runtime["status"] = "needs_human"
            runtime["terminal_reason"] = gate["reason"]
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
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0

        verifier_ids = _sorted_active_task_ids(tasks_payload, state_payload, role="verifier")
        if verifier_ids:
            task_id = verifier_ids[0]
            record = active[task_id]
            attempt = int(record.get("attempt") or 0)
            trial_commit = str(record.get("trial_commit") or "")
            _set_legacy_slot(state_payload, role="verifier", task_id=task_id, attempt=attempt, trial_commit=trial_commit)
            _write_state_payload(paths, state_payload)

            runtime["last_role"] = "verifier"
            runtime["last_task_id"] = task_id
            persist_runtime(paths.runtime, runtime)

            try:
                turn = run_role_turn(
                    manager=manager,
                    role="verifier",
                    task_id=task_id,
                    prompt=build_verifier_prompt(paths),
                    repo=paths.repo,
                    sandbox=sandbox_for_role("verifier", execution_policy),
                )
                with supervisor_lock:
                    decision = _apply_verifier_result(
                        paths=paths,
                        state_payload=state_payload,
                        tasks_payload=tasks_payload,
                        task_id=task_id,
                        report=turn["report"],
                    )
            except (HarnessError, AppServerError, OSError) as exc:
                runtime["status"] = "needs_human"
                runtime["terminal_reason"] = str(exc)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2

            runtime["last_decision"] = decision["decision"]
            runtime["last_reason"] = decision["reason"]
            persist_runtime(paths.runtime, runtime)

            if decision["decision"] == "stop":
                runtime["status"] = "terminal"
                runtime["terminal_reason"] = decision["reason"]
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 0
            if decision["decision"] == "needs_human":
                runtime["status"] = "needs_human"
                runtime["terminal_reason"] = decision["reason"]
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2
            time.sleep(args.sleep_seconds)
            continue

        planner_pending_reason = str(state_payload["state"].get("planner_pending_reason") or "")
        if planner_pending_reason and not active:
            state_payload["state"]["planner_pending_reason"] = ""
            _set_legacy_slot(state_payload, role="planner", task_id="", attempt=0, trial_commit="")
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
                runtime["status"] = "needs_human"
                runtime["terminal_reason"] = str(exc)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2

            runtime["last_decision"] = decision["decision"]
            runtime["last_reason"] = decision["reason"]
            persist_runtime(paths.runtime, runtime)

            if decision["decision"] == "stop":
                runtime["status"] = "terminal"
                runtime["terminal_reason"] = decision["reason"]
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 0
            if decision["decision"] == "needs_human":
                runtime["status"] = "needs_human"
                runtime["terminal_reason"] = decision["reason"]
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2
            time.sleep(args.sleep_seconds)
            continue

        tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
        state_payload = normalize_state_payload(read_json(paths.state))
        active = _active_tasks(state_payload)
        ready_tasks = _ready_tasks_not_active(tasks_payload, state_payload)

        if ready_tasks:
            for task in ready_tasks:
                active[str(task["id"])] = {
                    "role": "implementer",
                    "attempt": int(task.get("attempts", 0)) + 1,
                    "trial_commit": "",
                    "thread_id": "",
                    "verifier_feedback": "",
                }
            _write_state_payload(paths, state_payload)

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
                            runtime["status"] = "needs_human"
                            runtime["terminal_reason"] = errors[task_id]
                            persist_runtime(paths.runtime, runtime)
                            manager.shutdown()
                            return 2
                        if task_id not in results:
                            continue
                        state_payload = normalize_state_payload(read_json(paths.state))
                        tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
                        with supervisor_lock:
                            decision = _apply_implementer_result(
                                paths=paths,
                                state_payload=state_payload,
                                tasks_payload=tasks_payload,
                                task=task_index(tasks_payload)[task_id],
                                turn=results[task_id],
                            )
                    runtime["last_decision"] = decision["decision"]
                    runtime["last_reason"] = decision["reason"]
                    persist_runtime(paths.runtime, runtime)
                else:
                    task = batch_tasks[0]
                    task_id = str(task["id"])
                    record = _active_tasks(state_payload)[task_id]
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
                        repo=paths.repo,
                        sandbox=sandbox_for_role("implementer", execution_policy),
                        resume_thread_id=str(record.get("thread_id") or "") or None,
                    )
                    state_payload = normalize_state_payload(read_json(paths.state))
                    tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
                    with supervisor_lock:
                        decision = _apply_implementer_result(
                            paths=paths,
                            state_payload=state_payload,
                            tasks_payload=tasks_payload,
                            task=task_index(tasks_payload)[task_id],
                            turn=turn,
                        )
                    runtime["last_decision"] = decision["decision"]
                    runtime["last_reason"] = decision["reason"]
                    runtime["last_task_id"] = task_id
                    persist_runtime(paths.runtime, runtime)
            except (HarnessError, AppServerError, OSError) as exc:
                runtime["status"] = "needs_human"
                runtime["terminal_reason"] = str(exc)
                persist_runtime(paths.runtime, runtime)
                manager.shutdown()
                return 2

            time.sleep(args.sleep_seconds)
            continue

        tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
        state_payload = normalize_state_payload(read_json(paths.state))
        if all_tasks_done(tasks_payload):
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = "all_tasks_done"
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0

        _set_legacy_slot(state_payload, role="planner", task_id="", attempt=0, trial_commit="")
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
            runtime["status"] = "needs_human"
            runtime["terminal_reason"] = str(exc)
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 2

        runtime["last_decision"] = decision["decision"]
        runtime["last_reason"] = decision["reason"]
        persist_runtime(paths.runtime, runtime)

        if decision["decision"] == "stop":
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = decision["reason"]
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0
        if decision["decision"] == "needs_human":
            runtime["status"] = "needs_human"
            runtime["terminal_reason"] = decision["reason"]
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
    runtime["status"] = "stopped"
    runtime["terminal_reason"] = "user_stopped"
    persist_runtime(paths.runtime, runtime)
    return {"status": "stopped", "runtime_path": str(paths.runtime), "pid": pid, "pgid": pgid}
