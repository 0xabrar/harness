#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from harness_artifacts import HarnessError, Paths, default_paths, load_tasks, next_ready_task, read_json, refresh_ready_tasks, report_path_for_role


def state_context(paths: Paths) -> tuple[dict, dict]:
    state = read_json(paths.state)
    tasks = load_tasks(paths.tasks)
    return state, refresh_ready_tasks(tasks)


def build_planner_prompt(paths: Paths) -> str:
    state, tasks = state_context(paths)
    revision = int(tasks.get("planner_revision", 0)) + 1
    report_path = report_path_for_role(paths, "planner", planner_revision=revision)
    return f"""$harness
You are the planner role for this harness-managed repo.

The human already completed launch approval. Do not ask for more confirmation.

Goal: {state['config'].get('goal', '')}
Scope: {state['config'].get('scope', '')}
Plan path: {paths.plan}
Tasks path: {paths.tasks}
State path: {paths.state}
Events path: {paths.events}

Instructions:
- Read the repo, current plan, tasks, and any role reports in {paths.reports}.
- Update {paths.plan.name} and {paths.tasks.name}.
- You own the canonical task DAG.
- Add, split, reprioritize, or close tasks as needed.
- Every task must include explicit acceptance criteria.
- Do not write product code.
- Write a planner report to {report_path}.

The planner report must be JSON with:
- role = planner
- revision
- summary
- task_changes added/updated/closed arrays
- planner_requested_reason
"""


def build_implementer_prompt(paths: Paths) -> str:
    state, tasks = state_context(paths)
    current_task_id = str(state["state"].get("current_task_id") or "")
    current_attempt = int(state["state"].get("current_attempt") or 0)
    if current_task_id:
        task = next((item for item in tasks["tasks"] if str(item["id"]) == current_task_id), None)
    else:
        task = next_ready_task(tasks)
    if task is None:
        raise HarnessError("No task is available for the implementer.")
    attempt = current_attempt or (int(task.get("attempts", 0)) + 1)
    report_path = report_path_for_role(paths, "implementer", task_id=str(task["id"]), attempt=attempt)
    criteria = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    return f"""$harness
You are the implementer role for this harness-managed repo.

Assigned task: {task['id']} - {task['title']}
Description: {task['description']}
Acceptance criteria:
{criteria}

State path: {paths.state}
Tasks path: {paths.tasks}
Plan path: {paths.plan}
Reports dir: {paths.reports}

Instructions:
- Work only on this task.
- Do not edit tasks.json.
- Make code changes and create a single trial commit.
- Record the exact commit hash in the implementer report.
- Write the implementer report to {report_path}.

The implementer report must be JSON with:
- role = implementer
- task_id
- attempt
- commit
- summary
- files_changed
- checks_run
- proposed_tasks
"""


def build_verifier_prompt(paths: Paths) -> str:
    state = read_json(paths.state)
    task_id = str(state["state"].get("current_task_id") or "")
    attempt = int(state["state"].get("current_attempt") or 0)
    trial_commit = str(state["state"].get("trial_commit") or "")
    if not task_id or not attempt or not trial_commit:
        raise HarnessError("Verifier prompt requires current task, attempt, and trial commit in state.")
    tasks = refresh_ready_tasks(load_tasks(paths.tasks))
    task = next(task for task in tasks["tasks"] if str(task["id"]) == task_id)
    report_path = report_path_for_role(paths, "verifier", task_id=task_id, attempt=attempt)
    criteria = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    return f"""$harness
You are the verifier role for this harness-managed repo.

Task: {task_id} - {task['title']}
Trial commit: {trial_commit}
Acceptance criteria:
{criteria}

Instructions:
- Evaluate the exact commit {trial_commit}.
- Check the acceptance criteria and run appropriate validation.
- Do not modify tasks.json.
- Do not apply the revert yourself.
- Write a verifier report to {report_path}.

The verifier report must be JSON with:
- role = verifier
- task_id
- attempt
- commit
- verdict (accept, revert, or needs_human)
- summary
- findings
- criteria_results
- proposed_tasks
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the next role prompt for the harness runtime.")
    parser.add_argument("--repo")
    parser.add_argument("--role", required=True, choices=["planner", "implementer", "verifier"])
    args = parser.parse_args()
    paths = default_paths(args.repo)
    if args.role == "planner":
        print(build_planner_prompt(paths), end="")
    elif args.role == "implementer":
        print(build_implementer_prompt(paths), end="")
    else:
        print(build_verifier_prompt(paths), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as exc:
        raise SystemExit(f"error: {exc}")
