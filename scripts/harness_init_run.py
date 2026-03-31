#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness_artifacts import (
    DEFAULT_EXECUTION_POLICY,
    HarnessError,
    append_event,
    build_state_payload,
    default_paths,
    ensure_events_file,
    initial_plan_text,
    initial_tasks_payload,
    write_json_atomic,
    write_tasks,
)


def initialize_run(
    *,
    repo: str | Path | None,
    goal: str,
    scope: str,
    session_mode: str,
    execution_policy: str,
    run_tag: str = "",
    stop_condition: str = "",
    allow_task_expansion: str = "enabled",
    max_task_attempts: int = 3,
    force: bool = False,
) -> dict[str, str]:
    if session_mode != "background":
        raise HarnessError("Foreground mode is unsupported. Use session_mode='background'.")
    paths = default_paths(repo)
    existing = [path for path in (paths.state, paths.events, paths.tasks, paths.plan) if path.exists()]
    if existing and not force:
        raise HarnessError("Harness artifacts already exist. Use --force to overwrite.")
    if force:
        for path in (paths.state, paths.events, paths.tasks, paths.plan):
            if path.exists():
                path.unlink()
        if paths.reports.exists():
            for child in paths.reports.iterdir():
                if child.is_file():
                    child.unlink()

    config = {
        "goal": goal,
        "scope": scope,
        "session_mode": session_mode,
        "execution_policy": execution_policy,
        "stop_condition": stop_condition,
        "allow_task_expansion": allow_task_expansion,
        "max_task_attempts": max_task_attempts,
    }
    state = build_state_payload(config=config, run_tag=run_tag)
    write_json_atomic(paths.state, state)
    write_tasks(paths.tasks, initial_tasks_payload(goal))
    paths.plan.write_text(initial_plan_text(goal), encoding="utf-8")
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
    state["state"]["seq"] = 1
    write_json_atomic(paths.state, state)
    return {
        "state_path": str(paths.state),
        "events_path": str(paths.events),
        "tasks_path": str(paths.tasks),
        "plan_path": str(paths.plan),
        "reports_dir": str(paths.reports),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize harness state and working artifacts.")
    parser.add_argument("--repo")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--session-mode", choices=["background"], default="background")
    parser.add_argument("--execution-policy", choices=["workspace_write", "danger_full_access"], default=DEFAULT_EXECUTION_POLICY)
    parser.add_argument("--run-tag", default="")
    parser.add_argument("--stop-condition", default="")
    parser.add_argument("--allow-task-expansion", choices=["enabled", "disabled"], default="enabled")
    parser.add_argument("--max-task-attempts", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            initialize_run(
                repo=args.repo,
                goal=args.goal,
                scope=args.scope,
                session_mode=args.session_mode,
                execution_policy=args.execution_policy,
                run_tag=args.run_tag,
                stop_condition=args.stop_condition,
                allow_task_expansion=args.allow_task_expansion,
                max_task_attempts=args.max_task_attempts,
                force=args.force,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as exc:
        raise SystemExit(f"error: {exc}")
