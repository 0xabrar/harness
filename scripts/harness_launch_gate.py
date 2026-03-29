#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from harness_artifacts import HarnessError, default_paths, load_tasks, read_json
from harness_runtime_common import load_runtime, pid_is_alive


def evaluate_launch_context(*, repo: str | Path | None = None, ignore_running_runtime: bool = False) -> dict[str, Any]:
    paths = default_paths(repo)
    reasons: list[str] = []
    runtime = load_runtime(paths.runtime)
    runtime_running = bool(runtime and pid_is_alive(runtime.get("pid")))

    if runtime_running and not ignore_running_runtime:
        return {
            "decision": "blocked_start",
            "reason": "already_running",
            "runtime_running": True,
            "paths": {key: str(value) for key, value in paths.__dict__.items()},
            "reasons": ["A harness runtime is already active for this repo."],
        }

    launch_exists = paths.launch.exists()
    state_exists = paths.state.exists()
    tasks_exists = paths.tasks.exists()
    plan_exists = paths.plan.exists()
    events_exists = paths.events.exists()

    if not any((launch_exists, state_exists, tasks_exists, plan_exists, events_exists)):
        return {
            "decision": "fresh",
            "reason": "fresh_start",
            "runtime_running": False,
            "paths": {key: str(value) for key, value in paths.__dict__.items()},
            "reasons": ["No prior harness artifacts detected."],
        }

    if not launch_exists:
        reasons.append("Existing harness artifacts were found but harness-launch.json is missing.")
        return {
            "decision": "needs_human",
            "reason": "launch_manifest_missing",
            "runtime_running": False,
            "paths": {key: str(value) for key, value in paths.__dict__.items()},
            "reasons": reasons,
        }

    try:
        read_json(paths.launch)
        if state_exists:
            read_json(paths.state)
        if tasks_exists:
            load_tasks(paths.tasks)
    except HarnessError as exc:
        return {
            "decision": "needs_human",
            "reason": "invalid_artifact",
            "runtime_running": False,
            "paths": {key: str(value) for key, value in paths.__dict__.items()},
            "reasons": [str(exc)],
        }

    if not all((state_exists, tasks_exists, plan_exists, events_exists)):
        missing = [
            name
            for name, exists in (
                ("harness-state.json", state_exists),
                ("tasks.json", tasks_exists),
                ("plan.md", plan_exists),
                ("harness-events.tsv", events_exists),
            )
            if not exists
        ]
        return {
            "decision": "needs_human",
            "reason": "incomplete_artifacts",
            "runtime_running": False,
            "paths": {key: str(value) for key, value in paths.__dict__.items()},
            "reasons": [f"Incomplete harness artifacts: {', '.join(missing)}"],
        }

    return {
        "decision": "resumable",
        "reason": "full_resume",
        "runtime_running": runtime_running,
        "paths": {key: str(value) for key, value in paths.__dict__.items()},
        "reasons": ["Launch manifest and working artifacts are present."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect whether the harness run is fresh or resumable.")
    parser.add_argument("--repo")
    parser.add_argument("--ignore-running-runtime", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate_launch_context(repo=args.repo, ignore_running_runtime=args.ignore_running_runtime),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

