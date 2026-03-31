#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from harness_artifacts import HarnessError, Paths, default_paths, load_tasks, next_ready_task, read_json, refresh_ready_tasks


def state_context(paths: Paths) -> tuple[dict, dict]:
    state = read_json(paths.state)
    tasks = load_tasks(paths.tasks)
    return state, refresh_ready_tasks(tasks)


def build_planner_prompt(paths: Paths) -> str:
    state, tasks = state_context(paths)
    revision = int(tasks.get("planner_revision", 0)) + 1
    return f"""$harness
You are the planner role for this harness-managed repo.

The human already completed launch approval. Do not ask for more confirmation.

<task>
Goal: {state['config'].get('goal', '')}
Scope: {state['config'].get('scope', '')}

Read the repo, current plan, tasks, and any role reports in {paths.reports}.
Update {paths.plan.name} and {paths.tasks.name}. You own the canonical task DAG.
</task>

<paths>
Plan: {paths.plan}
Tasks: {paths.tasks}
State: {paths.state}
Events: {paths.events}
Reports: {paths.reports}
</paths>

<task_design>
Each task should be small enough for one implementer turn — roughly one file or one focused feature.

Every task must have explicit, testable acceptance criteria. "Works correctly" is not a criterion. "greet('World') returns 'Hello, World!'" is.

Mark tasks with no mutual dependencies as independent so the runtime can parallelize them.
</task_design>

<constraints>
Add, split, reprioritize, or close tasks as needed.
Do not write product code.
Do not guess at implementation details you have not verified by reading the repo.
If prior reports in {paths.reports} show repeated failures on a task, consider splitting it or rewriting its acceptance criteria.
</constraints>

<output>
Return your report as structured JSON as your final response. The runtime captures it via outputSchema. Do not write report files to disk.

Fields: role, revision, summary, task_changes (added/updated/closed arrays), planner_requested_reason.
</output>
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
    criteria = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    return _implementer_prompt_body(paths, task, attempt, criteria)


def build_implementer_prompt_for_task(paths: Paths, task: dict) -> str:
    """Build an implementer prompt for a specific task (used by parallel execution)."""
    attempt = int(task.get("attempts", 0)) + 1
    criteria = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    return _implementer_prompt_body(paths, task, attempt, criteria)


def _implementer_prompt_body(paths: Paths, task: dict, attempt: int, criteria: str) -> str:
    return f"""$harness
You are the implementer role for this harness-managed repo.

<task>
Assigned task: {task['id']} - {task['title']}
Description: {task['description']}
Attempt: {attempt}

Acceptance criteria:
{criteria}
</task>

<paths>
State: {paths.state}
Tasks: {paths.tasks}
Plan: {paths.plan}
Reports: {paths.reports}
</paths>

<constraints>
Work only on this task. Do not edit tasks.json.
Only touch files related to this task. If you notice other issues, put them in proposed_tasks — do not fix them yourself.
Create exactly one commit. Record the full commit hash in your report.
</constraints>

<verification>
Before committing, verify your work:
- If there are existing tests, run them.
- If you wrote new code, run it or import it to confirm it does not crash.
- If an acceptance criterion is checkable from the command line, check it.
Record what you ran in checks_run so the verifier knows what was already validated.
</verification>

<output>
Return your report as structured JSON as your final response. The runtime captures it via outputSchema. Do not write report files to disk.

Fields: role, task_id, attempt, commit, summary, files_changed, checks_run, proposed_tasks.
</output>
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
    criteria = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    return f"""$harness
You are the verifier role for this harness-managed repo. Default to skepticism — you are here to verify, not to approve.

<task>
Task: {task_id} - {task['title']}
Trial commit: {trial_commit}

Acceptance criteria:
{criteria}
</task>

<verification>
Do real work to verify the commit:
- Inspect the actual diff with git show {trial_commit}.
- Run the tests or validation commands yourself. Do not assume the implementer ran them correctly.
- Check each acceptance criterion individually. Record pass, fail, or skip with the evidence you gathered.
- If a criterion says "function returns X", call the function and check.
</verification>

<verdicts>
accept: Every criterion passes with evidence you gathered yourself.
revert: Any criterion fails, or the commit introduces obvious breakage or touches unrelated files. Be specific about what failed — the implementer will retry with your feedback.
needs_human: You cannot verify a criterion because the environment prevents it, or the acceptance criteria are ambiguous.
</verdicts>

<constraints>
Do not modify code. Do not apply reverts yourself. Do not edit tasks.json. Do not write report files to disk.
</constraints>

<output>
Return your report as structured JSON as your final response. The runtime captures it via outputSchema.

Fields: role, task_id, attempt, commit, verdict (accept/revert/needs_human), summary, findings, criteria_results, proposed_tasks.
</output>
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
