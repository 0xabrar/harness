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
    build_launch_manifest,
    build_runtime_payload,
    default_paths,
    load_tasks,
    read_json,
    refresh_ready_tasks,
    task_index,
    write_json_atomic,
)
from harness_init_run import initialize_run
from harness_build_prompt import build_implementer_prompt, build_implementer_prompt_for_task, build_planner_prompt, build_verifier_prompt
from harness_launch_gate import evaluate_launch_context
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
            prompt = build_implementer_prompt_for_task(paths, task)
            turn = run_role_turn(
                manager=manager,
                role="implementer",
                task_id=task_id,
                prompt=prompt,
                repo=paths.repo,
                sandbox=sandbox_for_role("implementer", execution_policy),
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


def run_runtime(args: argparse.Namespace) -> int:
    paths = default_paths(args.repo)
    runtime = read_json(paths.runtime) if paths.runtime.exists() else build_runtime_payload(paths=paths, status="running")
    persist_runtime(paths.runtime, runtime)
    launch = read_json(paths.launch)
    execution_policy = str(launch.get("config", {}).get("execution_policy") or DEFAULT_EXECUTION_POLICY)

    codex_bin = getattr(args, "codex_bin", "codex")
    manager = ServerManager(cwd=str(paths.repo), codex_bin=codex_bin)
    manager.kill_orphans()

    task_thread_map: dict[str, str] = {}  # task_id -> last thread_id
    last_verifier_feedback: str = ""  # verifier summary from the most recent revert
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

        state = read_json(paths.state)
        role = str(state["state"].get("current_role") or "")
        task_id = str(state["state"].get("current_task_id") or "")
        prompt_text = prompt_for_role(paths, role)

        # On implementer retry, prepend verifier feedback so the agent
        # knows *why* the previous attempt was reverted.
        resume_id: str | None = None
        if role == "implementer" and task_id in task_thread_map:
            resume_id = task_thread_map[task_id]
            if last_verifier_feedback:
                prompt_text = (
                    f"[VERIFIER FEEDBACK FROM PREVIOUS ATTEMPT]\n"
                    f"{last_verifier_feedback}\n"
                    f"[END VERIFIER FEEDBACK]\n\n"
                    f"{prompt_text}"
                )
                last_verifier_feedback = ""

        runtime["last_role"] = role
        runtime["last_task_id"] = task_id
        persist_runtime(paths.runtime, runtime)

        try:
            turn = run_role_turn(
                manager=manager,
                role=role,
                task_id=task_id,
                prompt=prompt_text,
                repo=paths.repo,
                sandbox=sandbox_for_role(role, execution_policy),
                resume_thread_id=resume_id,
            )

            # Track thread for potential future resume
            if role == "implementer" and task_id:
                task_thread_map[task_id] = turn["thread_id"]

            report = turn["report"]
            with supervisor_lock:
                decision = evaluate_supervisor_status(repo=paths.repo, report_override=report)
        except (HarnessError, AppServerError, OSError) as exc:
            runtime["status"] = "needs_human"
            runtime["terminal_reason"] = str(exc)
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 2

        # After a verifier revert that triggers a retry, capture the
        # feedback so the next implementer turn can see it.
        if decision.get("reason") == "retry_task":
            last_verifier_feedback = str(report.get("summary") or "")

        runtime["last_decision"] = decision["decision"]
        runtime["last_reason"] = decision["reason"]
        persist_runtime(paths.runtime, runtime)

        if decision["decision"] == "relaunch" and decision["reason"] == "dispatch_implementer":
            tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
            ready = all_ready_tasks(tasks_payload)
            if len(ready) > 1:
                # Run parallel implementers, then process results sequentially
                try:
                    results, errors = _run_parallel_implementers(
                        manager=manager,
                        ready_tasks=ready,
                        paths=paths,
                        runtime=runtime,
                        execution_policy=execution_policy,
                    )
                except (HarnessError, AppServerError, OSError) as exc:
                    runtime["status"] = "needs_human"
                    runtime["terminal_reason"] = str(exc)
                    persist_runtime(paths.runtime, runtime)
                    manager.shutdown()
                    return 2

                # Process results sequentially — state transitions must be serial
                tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
                idx = task_index(tasks_payload)
                for task in ready:
                    task_id = str(task["id"])
                    if task_id in errors:
                        runtime["status"] = "needs_human"
                        runtime["terminal_reason"] = errors[task_id]
                        persist_runtime(paths.runtime, runtime)
                        manager.shutdown()
                        return 2
                    if task_id not in results:
                        continue

                    task_thread_map[task_id] = results[task_id]["thread_id"]

                    # Set the correct task context so evaluate_supervisor_status
                    # reads the right current_task_id / current_attempt / role.
                    par_task = idx[task_id]
                    state_payload = read_json(paths.state)
                    state_payload["state"]["current_role"] = "implementer"
                    state_payload["state"]["current_task_id"] = task_id
                    state_payload["state"]["current_attempt"] = int(par_task.get("attempts", 0)) + 1
                    write_json_atomic(paths.state, state_payload)

                    par_report = results[task_id]["report"]
                    try:
                        with supervisor_lock:
                            par_decision = evaluate_supervisor_status(
                                repo=paths.repo, report_override=par_report
                            )
                    except (HarnessError, AppServerError) as exc:
                        runtime["status"] = "needs_human"
                        runtime["terminal_reason"] = str(exc)
                        persist_runtime(paths.runtime, runtime)
                        manager.shutdown()
                        return 2

                    runtime["last_decision"] = par_decision["decision"]
                    runtime["last_reason"] = par_decision["reason"]
                    persist_runtime(paths.runtime, runtime)

                    if par_decision["decision"] == "stop":
                        runtime["status"] = "terminal"
                        runtime["terminal_reason"] = par_decision["reason"]
                        persist_runtime(paths.runtime, runtime)
                        manager.shutdown()
                        return 0
                    if par_decision["decision"] == "needs_human":
                        runtime["status"] = "needs_human"
                        runtime["terminal_reason"] = par_decision["reason"]
                        persist_runtime(paths.runtime, runtime)
                        manager.shutdown()
                        return 2

                time.sleep(args.sleep_seconds)
                continue
            else:
                # Single task — continue existing sequential flow
                time.sleep(args.sleep_seconds)
                continue

        if decision["decision"] == "relaunch":
            time.sleep(args.sleep_seconds)
            continue
        if decision["decision"] == "stop":
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = decision["reason"]
            persist_runtime(paths.runtime, runtime)
            manager.shutdown()
            return 0
        runtime["status"] = "needs_human"
        runtime["terminal_reason"] = decision["reason"]
        persist_runtime(paths.runtime, runtime)
        manager.shutdown()
        return 2


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
