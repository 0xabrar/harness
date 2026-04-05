#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from harness_artifacts import (
    HarnessError,
    Paths,
    default_paths,
    load_tasks,
    next_ready_task,
    normalize_state_payload,
    read_json,
    refresh_ready_tasks,
    report_path_for_role,
    task_index,
)


def state_context(paths: Paths) -> tuple[dict, dict]:
    state = normalize_state_payload(read_json(paths.state))
    tasks = load_tasks(paths.tasks)
    return state, refresh_ready_tasks(tasks)


def _task_sort_key(task: dict) -> tuple[int, str]:
    return (int(task.get("priority", 100)), str(task["id"]))


def _active_task_record(state: dict, tasks: dict, *, role: str) -> tuple[dict, dict] | tuple[None, None]:
    active = state.get("state", {}).get("active_tasks", {})
    if not isinstance(active, dict):
        return None, None
    index = task_index(tasks)
    matching_ids = [task_id for task_id, record in active.items() if str(record.get("role") or "") == role]
    if not matching_ids:
        return None, None
    matching_ids.sort(key=lambda task_id: _task_sort_key(index.get(task_id, {"id": task_id, "priority": 100})))
    task_id = matching_ids[0]
    task = index.get(task_id)
    if task is None:
        raise HarnessError(f"Active task {task_id!r} is missing from tasks.json.")
    return task, active[task_id]


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

Mark tasks with no mutual dependencies as independent so the runtime can parallelize them in isolated task worktrees.
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

Use this exact shape:
{{
  "role": "planner",
  "revision": {revision},
  "summary": "Brief planning summary.",
  "task_changes": {{
    "added": ["T-001"],
    "updated": [],
    "closed": []
  }},
  "planner_requested_reason": "initial_plan"
}}
</output>
"""


def build_implementer_prompt(paths: Paths) -> str:
    state, tasks = state_context(paths)
    task, record = _active_task_record(state, tasks, role="implementer")
    if task is None:
        task = next_ready_task(tasks)
        record = {}
    if task is None:
        raise HarnessError("No task is available for the implementer.")
    attempt = int((record or {}).get("attempt") or (int(task.get("attempts", 0)) + 1))
    criteria = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    return _implementer_prompt_body(paths, task, attempt, criteria)


def build_implementer_prompt_for_task(paths: Paths, task: dict, *, attempt: int | None = None) -> str:
    """Build an implementer prompt for a specific task."""
    attempt = attempt or (int(task.get("attempts", 0)) + 1)
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

Use this exact shape:
{{
  "role": "implementer",
  "task_id": "{task['id']}",
  "attempt": {attempt},
  "commit": "full git commit sha",
  "summary": "Brief implementation summary.",
  "files_changed": ["path/to/file"],
  "checks_run": ["command you ran"],
  "proposed_tasks": []
}}
</output>
"""


def build_verifier_prompt(
    paths: Paths,
    *,
    task_id: str | None = None,
    attempt: int | None = None,
    trial_commit: str | None = None,
) -> str:
    state = normalize_state_payload(read_json(paths.state))
    tasks = refresh_ready_tasks(load_tasks(paths.tasks))
    if task_id:
        task = task_index(tasks).get(str(task_id))
        if task is None:
            raise HarnessError(f"Task {task_id!r} is missing from tasks.json.")
        record = state["state"].get("active_tasks", {}).get(str(task_id), {})
    else:
        task, record = _active_task_record(state, tasks, role="verifier")
        if task is None:
            raise HarnessError("Verifier prompt requires an active verifier task.")
    task_id = str(task["id"])
    attempt = int(attempt or record.get("attempt") or 0)
    trial_commit = str(trial_commit or record.get("trial_commit") or "")
    if not attempt or not trial_commit:
        raise HarnessError("Verifier prompt requires task attempt and trial commit.")
    implementer_report_path = report_path_for_role(paths, "implementer", task_id=task_id, attempt=attempt)
    try:
        implementer_report = read_json(implementer_report_path)
    except Exception:
        implementer_report = {}
    files_changed = [str(item) for item in implementer_report.get("files_changed") or []]
    checks_run = [str(item) for item in implementer_report.get("checks_run") or []]
    criteria = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    files_section = "\n".join(f"- {item}" for item in files_changed) or "- none reported"
    checks_section = "\n".join(f"- {item}" for item in checks_run) or "- none reported"
    return f"""$harness
You are the verifier role for this harness-managed repo. Default to skepticism — you are here to verify, not to approve.

<task>
Task: {task_id} - {task['title']}
Trial commit: {trial_commit}

Acceptance criteria:
{criteria}
</task>

<implementer_report>
Summary: {implementer_report.get('summary', '')}
Files changed:
{files_section}

Checks already run by implementer:
{checks_section}
</implementer_report>

<verification>
Do only the minimum real work needed to verify the commit:
- Inspect the exact diff with `git show --stat --name-only {trial_commit}`.
- Focus on the files listed above. Do not do broad repo exploration.
- Prefer direct checks against the committed content such as `git show {trial_commit}:path/to/file`.
- Re-run only the smallest commands needed to confirm each criterion yourself.
- Keep verification tight. In normal cases use no more than 5 shell commands.
- Check each acceptance criterion individually and record pass, fail, or skip with evidence you gathered.
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

Use these exact shapes:
- findings: array of objects with keys description, severity, file, recommendation
- criteria_results: array of objects with keys criterion, result, evidence
- proposed_tasks: array of objects with keys title, reason, depends_on, introduced_by
- If there are no findings or proposed tasks, return [] for those arrays.

Example shape:
{{
  "role": "verifier",
  "task_id": "{task_id}",
  "attempt": {attempt},
  "commit": "{trial_commit}",
  "verdict": "accept",
  "summary": "Brief verification summary.",
  "findings": [],
  "criteria_results": [
    {{
      "criterion": "Criterion text",
      "result": "pass",
      "evidence": "What you checked."
    }}
  ],
  "proposed_tasks": []
}}
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
