#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness_artifacts import DEFAULT_EXECUTION_POLICY, HarnessError, default_paths
from harness_runtime_common import runtime_summary
from harness_runtime_ops import launch_and_start_runtime, run_runtime, start_runtime, stop_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control the harness runtime controller.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-launch")
    create.add_argument("--repo")
    create.add_argument("--original-goal", required=True)
    create.add_argument("--prompt-text")
    create.add_argument("--goal", required=True)
    create.add_argument("--scope", required=True)
    create.add_argument("--execution-policy", choices=["workspace_write", "danger_full_access"], default=DEFAULT_EXECUTION_POLICY)
    create.add_argument("--stop-condition")
    create.add_argument("--allow-task-expansion", choices=["enabled", "disabled"], default="enabled")
    create.add_argument("--max-task-attempts", type=int, default=3)
    create.add_argument("--note", action="append", default=[])

    launch = sub.add_parser("launch")
    for arg in create._actions[1:]:
        if arg.dest == "help":
            continue
        launch._add_action(arg)
    launch.add_argument("--codex-bin", default="codex")
    launch.add_argument("--sleep-seconds", type=int, default=5)

    start = sub.add_parser("start")
    start.add_argument("--repo")
    start.add_argument("--codex-bin", default="codex")
    start.add_argument("--sleep-seconds", type=int, default=5)

    run = sub.add_parser("run")
    run.add_argument("--repo")
    run.add_argument("--codex-bin", default="codex")
    run.add_argument("--sleep-seconds", type=int, default=5)

    status = sub.add_parser("status")
    status.add_argument("--repo")

    stop = sub.add_parser("stop")
    stop.add_argument("--repo")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    runner_path = Path(__file__).resolve()
    if args.command == "create-launch":
        from harness_runtime_ops import create_launch_manifest

        print(json.dumps(create_launch_manifest(args), indent=2, sort_keys=True))
        return 0
    if args.command == "launch":
        print(json.dumps(launch_and_start_runtime(args, runner_path=runner_path), indent=2, sort_keys=True))
        return 0
    if args.command == "start":
        print(json.dumps(start_runtime(args, runner_path=runner_path), indent=2, sort_keys=True))
        return 0
    if args.command == "run":
        return run_runtime(args)
    if args.command == "status":
        print(json.dumps(runtime_summary(default_paths(args.repo)), indent=2, sort_keys=True))
        return 0
    if args.command == "stop":
        print(json.dumps(stop_runtime(args), indent=2, sort_keys=True))
        return 0
    raise HarnessError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as exc:
        raise SystemExit(f"error: {exc}")
