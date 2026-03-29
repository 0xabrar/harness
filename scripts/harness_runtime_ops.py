#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harness_artifacts import (
    DEFAULT_EXECUTION_POLICY,
    HarnessError,
    Paths,
    build_launch_manifest,
    build_runtime_payload,
    default_paths,
    read_json,
    write_json_atomic,
)
from harness_init_run import initialize_run
from harness_build_prompt import build_implementer_prompt, build_planner_prompt, build_verifier_prompt
from harness_launch_gate import evaluate_launch_context
from harness_runtime_common import codex_args_for_execution_policy, ensure_runtime_not_running, persist_runtime, runtime_summary
from harness_supervisor_status import evaluate_supervisor_status


def build_codex_exec_command(*, codex_bin: str, codex_args: list[str], repo: Path) -> list[str]:
    return [codex_bin, "exec", *codex_args, "-C", str(repo), "-"]


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
    for value in args.codex_arg:
        command.extend(["--codex-arg", value])
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
    codex_args = codex_args_for_execution_policy(execution_policy, extra_args=args.codex_arg)

    while True:
        gate = evaluate_launch_context(repo=paths.repo, ignore_running_runtime=True)
        if gate["decision"] not in {"fresh", "resumable"}:
            runtime["status"] = "needs_human"
            runtime["terminal_reason"] = gate["reason"]
            persist_runtime(paths.runtime, runtime)
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
        prompt_text = prompt_for_role(paths, role)
        runtime["last_role"] = role
        runtime["last_task_id"] = str(state["state"].get("current_task_id") or "")
        persist_runtime(paths.runtime, runtime)
        codex_cmd = build_codex_exec_command(codex_bin=args.codex_bin, codex_args=codex_args, repo=paths.repo)
        try:
            codex_exit = subprocess.run(codex_cmd, cwd=paths.repo, input=prompt_text, text=True).returncode
        except OSError as exc:
            runtime["status"] = "needs_human"
            runtime["terminal_reason"] = "codex_exec_unavailable"
            runtime["last_error"] = str(exc)
            persist_runtime(paths.runtime, runtime)
            return 2

        decision = evaluate_supervisor_status(repo=paths.repo)
        runtime["last_decision"] = decision["decision"]
        runtime["last_reason"] = decision["reason"]
        persist_runtime(paths.runtime, runtime)

        if decision["decision"] == "relaunch":
            time.sleep(args.sleep_seconds)
            continue
        if decision["decision"] == "stop":
            runtime["status"] = "terminal"
            runtime["terminal_reason"] = decision["reason"]
            persist_runtime(paths.runtime, runtime)
            return 0 if codex_exit == 0 else 1
        runtime["status"] = "needs_human"
        runtime["terminal_reason"] = decision["reason"]
        persist_runtime(paths.runtime, runtime)
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
