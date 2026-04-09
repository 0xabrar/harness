#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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


def _resume_target(*, role: str, task_id: str, attempt: int) -> str:
    parts: list[str] = []
    if role:
        parts.append(f"role={role}")
    if task_id:
        parts.append(f"task={task_id}")
    if attempt:
        parts.append(f"attempt={attempt}")
    return ", ".join(parts) or "none"


def _planner_recovery_context(state: dict, tasks: dict) -> str:
    state_block = state.get("state", {})
    recovery = state_block.get("recovery", {})
    sections: list[str] = []

    if str(recovery.get("status") or "") == "pending":
        owner = str(recovery.get("owner") or "unknown")
        reason = str(recovery.get("reason") or "unknown")
        resume_target = _resume_target(
            role=str(recovery.get("resume_role") or ""),
            task_id=str(recovery.get("resume_task_id") or ""),
            attempt=int(recovery.get("resume_attempt") or 0),
        )
        lines = [
            "<recovery_context>",
            "Recovery is pending. Repair the DAG so the runtime can resume safely.",
            f"Recovery owner: {owner}",
            f"Recovery reason: {reason}",
            f"Resume target: {resume_target}",
        ]

        incident = recovery.get("incident") or {}
        if any(
            incident.get(key)
            for key in ("owner", "reason", "resume_role", "resume_task_id", "resume_attempt", "commit", "details")
        ):
            lines.extend(
                [
                    "Incident details:",
                    f"- owner: {str(incident.get('owner') or owner)}",
                    f"- reason: {str(incident.get('reason') or reason)}",
                    "- resume target: "
                    + _resume_target(
                        role=str(incident.get("resume_role") or ""),
                        task_id=str(incident.get("resume_task_id") or ""),
                        attempt=int(incident.get("resume_attempt") or 0),
                    ),
                    f"- commit: {str(incident.get('commit') or 'none')}",
                    "- details: "
                    + json.dumps(dict(incident.get("details") or {}), sort_keys=True),
                ]
            )
            incident_reason = str(incident.get("reason") or reason)
            incident_task_id = str(incident.get("resume_task_id") or recovery.get("resume_task_id") or "")
            if incident_reason == "integration_conflict" and incident_task_id:
                lines.extend(
                    [
                        "Integration-conflict repair rules:",
                        "- Keep the original conflicted task unfinished until a repair task is accepted and integrated.",
                        "- Create a dedicated repair task that runs against the current main branch in a fresh worktree.",
                        f'- Add `repair_target_task_id: "{incident_task_id}"` to that repair task so the runtime can close the original task after verification.',
                        "- Do not make the blocked task a dependency of the repair task.",
                    ]
                )

        retry = recovery.get("retry") or {}
        if any(retry.get(key) for key in ("count", "reason", "resume_role", "resume_task_id", "resume_attempt")):
            lines.extend(
                [
                    "Retry details:",
                    f"- count: {int(retry.get('count') or 0)}",
                    f"- reason: {str(retry.get('reason') or reason)}",
                    "- resume target: "
                    + _resume_target(
                        role=str(retry.get("resume_role") or ""),
                        task_id=str(retry.get("resume_task_id") or ""),
                        attempt=int(retry.get("resume_attempt") or 0),
                    ),
                ]
            )

        lines.extend(
            [
                "Planner action:",
                "- Create repair or sequencing tasks for semantic recovery when the current DAG cannot safely continue.",
                "- Re-sequence, split, or add explicit unblockers instead of leaving the affected work blocked with no ready path.",
                "</recovery_context>",
                "",
            ]
        )
        sections.append("\n".join(lines))

    pending_reason = str(state_block.get("planner_pending_reason") or "")
    failed_tasks = [task for task in tasks.get("tasks", []) if str(task.get("status") or "") == "failed"]
    if pending_reason == "planner_replan_after_revert" and failed_tasks:
        lines = [
            "<retry_context>",
            "A task exhausted its implementation retries and needs planner repair.",
            f"Planner requested reason: {pending_reason}",
            "Failed task snapshots:",
        ]
        for task in sorted(failed_tasks, key=_task_sort_key):
            lines.append(
                f"- {task['id']}: attempts={int(task.get('attempts', 0))}, "
                f"last_verdict={str(task.get('last_verdict') or 'unknown')}, "
                f"last_attempt_commit={str(task.get('last_attempt_commit') or 'none')}, "
                f"blocked_reason={str(task.get('blocked_reason') or 'none')}"
            )
        lines.extend(
            [
                "Planner action:",
                "- Replace, split, or sequence follow-up tasks so the failed work no longer blocks the DAG.",
                "- Rewrite acceptance criteria if repeated retries exposed ambiguity or an unsafe task boundary.",
                "</retry_context>",
                "",
            ]
        )
        sections.append("\n".join(lines))

    return "".join(sections)


def build_planner_prompt(paths: Paths) -> str:
    state, tasks = state_context(paths)
    revision = int(tasks.get("planner_revision", 0)) + 1
    recovery_context = _planner_recovery_context(state, tasks)
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

{recovery_context}<task_design>
Each task should be small enough for one implementer turn — roughly one file or one focused feature.

Every task must have explicit, testable acceptance criteria. "Works correctly" is not a criterion. "greet('World') returns 'Hello, World!'" is.

Mark tasks with no mutual dependencies as independent so the runtime can parallelize them in isolated task worktrees.
</task_design>

<constraints>
Add, split, reprioritize, or close tasks as needed.
Do not write product code.
Do not guess at implementation details you have not verified by reading the repo.
If prior reports in {paths.reports} show repeated failures on a task, consider splitting it or rewriting its acceptance criteria.
When recovery context is present, convert it into concrete repair or sequencing tasks instead of leaving the run blocked.
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
    except (HarnessError, FileNotFoundError):
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
revert: Any criterion fails, or the commit introduces obvious breakage or touches unrelated files. Also use revert when verification cannot complete and you need planner-owned recovery.
</verdicts>

<recovery_signals>
none: Normal verification completed. Use this for ordinary accept/revert outcomes.
environment_blocked: The environment prevented deterministic verification.
ambiguous_acceptance_criteria: The acceptance criteria are too ambiguous to verify safely.
</recovery_signals>

<constraints>
Do not modify code. Do not apply reverts yourself. Do not edit tasks.json. Do not write report files to disk.
</constraints>

<output>
Return your report as structured JSON as your final response. The runtime captures it via outputSchema.

Fields: role, task_id, attempt, commit, verdict (accept/revert), recovery_signal (none/environment_blocked/ambiguous_acceptance_criteria), summary, findings, criteria_results, proposed_tasks.

Use these exact shapes:
- findings: array of objects with keys description, severity, file, recommendation
- criteria_results: array of objects with keys criterion, result, evidence
- proposed_tasks: array of objects with keys title, reason, depends_on, introduced_by
- If recovery_signal is not none, set verdict to revert and explain the blocker in summary and criteria_results.
- If there are no findings or proposed tasks, return [] for those arrays.

Example shape:
{{
  "role": "verifier",
  "task_id": "{task_id}",
  "attempt": {attempt},
  "commit": "{trial_commit}",
  "verdict": "accept",
  "recovery_signal": "none",
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
