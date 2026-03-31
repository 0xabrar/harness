#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from harness_artifacts import (
    HarnessError,
    append_event,
    all_tasks_done,
    default_paths,
    git_head_commit,
    load_tasks,
    next_ready_task,
    read_json,
    refresh_ready_tasks,
    report_path_for_role,
    task_index,
    utc_now,
    write_json_atomic,
    write_tasks,
)
from harness_lessons import append_lesson


def context_string(state: dict[str, Any]) -> str:
    config = state.get("config", {})
    return (
        f"goal={config.get('goal', '')}; "
        f"scope={config.get('scope', '')}; "
        f"role={state['state'].get('current_role', '')}"
    )


def _append_summary_lesson(paths, state_payload: dict[str, Any], outcome: str, insight: str) -> None:
    append_lesson(
        path=paths.lessons,
        title=f"Run summary: {state_payload['config'].get('goal', 'Harness run')}",
        category="summary",
        strategy="Runtime completion summary",
        outcome=outcome,
        insight=insight,
        context=context_string(state_payload),
        iteration=str(state_payload["state"].get("seq", 0)),
    )


def revert_trial_commit(repo: Path, trial_commit: str) -> None:
    head = git_head_commit(repo)
    if head == trial_commit:
        completed = subprocess.run(["git", "-C", str(repo), "reset", "--hard", "HEAD~1"], text=True, capture_output=True, check=False)
        if completed.returncode == 0:
            return
    completed = subprocess.run(["git", "-C", str(repo), "revert", "--no-edit", trial_commit], text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise HarnessError(completed.stderr.strip() or f"Failed to revert commit {trial_commit}")


def planner_report_state(paths, state_payload: dict[str, Any], tasks_payload: dict[str, Any], *, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    current_revision = int(state_payload["state"].get("planner_revision", 0))
    report_path = report_path_for_role(paths, "planner", planner_revision=current_revision + 1)
    if report_override is not None:
        report = report_override
        write_json_atomic(report_path, report)  # persist for audit
    else:
        if not report_path.exists():
            raise HarnessError(f"Planner report missing: {report_path}")
        report = read_json(report_path)
    revision = max(
        int(tasks_payload.get("planner_revision", 0)),
        int(report.get("revision") or report.get("plan_revision") or (current_revision + 1)),
    )
    state_payload["state"]["planner_revision"] = revision
    state_payload["state"]["planner_runs"] += 1
    state_payload["state"]["last_status"] = "plan"

    tasks_payload = refresh_ready_tasks(tasks_payload)
    if all_tasks_done(tasks_payload):
        state_payload["state"]["completed"] = True
        state_payload["state"]["current_role"] = ""
        state_payload["state"]["current_task_id"] = ""
        state_payload["state"]["current_attempt"] = 0
        return {"decision": "stop", "reason": "all_tasks_done", "report": report, "tasks": tasks_payload}

    task = next_ready_task(tasks_payload)
    if task is None:
        state_payload["state"]["needs_human"] += 1
        return {
            "decision": "needs_human",
            "reason": "planner_left_no_ready_tasks",
            "report": report,
            "tasks": tasks_payload,
        }

    state_payload["state"]["current_role"] = "implementer"
    state_payload["state"]["current_task_id"] = str(task["id"])
    state_payload["state"]["current_attempt"] = int(task.get("attempts", 0)) + 1
    return {"decision": "relaunch", "reason": "dispatch_implementer", "report": report, "tasks": tasks_payload}


def implementer_report_state(paths, state_payload: dict[str, Any], tasks_payload: dict[str, Any], *, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    task_id = str(state_payload["state"].get("current_task_id") or "")
    attempt = int(state_payload["state"].get("current_attempt") or 0)
    if not task_id or not attempt:
        raise HarnessError("Implementer state is missing current task or attempt.")
    report_path = report_path_for_role(paths, "implementer", task_id=task_id, attempt=attempt)
    if report_override is not None:
        report = report_override
        write_json_atomic(report_path, report)  # persist for audit
    else:
        if not report_path.exists():
            raise HarnessError(f"Implementer report missing: {report_path}")
        report = read_json(report_path)
    commit = str(report.get("commit") or report.get("trial_commit") or "")
    if not commit:
        raise HarnessError("Implementer report must include a commit.")

    index = task_index(tasks_payload)
    task = index[task_id]
    task["status"] = "in_progress"
    task["attempts"] = attempt
    task["last_attempt_commit"] = commit
    tasks_payload["updated_at"] = tasks_payload.get("updated_at")

    state_payload["state"]["implementer_runs"] += 1
    state_payload["state"]["trial_commit"] = commit
    state_payload["state"]["last_status"] = "submit_trial"
    state_payload["state"]["current_role"] = "verifier"
    return {"decision": "relaunch", "reason": "dispatch_verifier", "report": report, "tasks": tasks_payload}


def verifier_report_state(paths, state_payload: dict[str, Any], tasks_payload: dict[str, Any], *, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    task_id = str(state_payload["state"].get("current_task_id") or "")
    attempt = int(state_payload["state"].get("current_attempt") or 0)
    trial_commit = str(state_payload["state"].get("trial_commit") or "")
    if not task_id or not attempt or not trial_commit:
        raise HarnessError("Verifier state is missing current task, attempt, or trial commit.")
    verdict_path = report_path_for_role(paths, "verifier", task_id=task_id, attempt=attempt)
    impl_path = report_path_for_role(paths, "implementer", task_id=task_id, attempt=attempt)
    if report_override is not None:
        verdict = report_override
        write_json_atomic(verdict_path, verdict)  # persist for audit
    else:
        if not verdict_path.exists():
            raise HarnessError(f"Verifier report missing: {verdict_path}")
        verdict = read_json(verdict_path)
    implementer = read_json(impl_path)
    index = task_index(tasks_payload)
    task = index[task_id]

    verdict_value = str(verdict.get("verdict") or "")
    if verdict_value not in {"accept", "revert", "needs_human"}:
        raise HarnessError("Verifier verdict must be accept, revert, or needs_human.")

    proposed_tasks = list(implementer.get("proposed_tasks") or []) + list(verdict.get("proposed_tasks") or [])
    state_payload["state"]["verifier_runs"] += 1
    state_payload["state"]["last_verdict"] = verdict_value
    state_payload["state"]["last_status"] = verdict_value

    if verdict_value == "accept":
        task["status"] = "done"
        task["last_verdict"] = "accept"
        state_payload["state"]["accepts"] += 1
        state_payload["state"]["accepted_commit"] = trial_commit
        state_payload["state"]["trial_commit"] = ""
        append_lesson(
            path=paths.lessons,
            title=f"Accepted task {task_id}",
            category="task",
            strategy=str(implementer.get("summary") or f"Completed {task_id}"),
            outcome="accept",
            insight=str(verdict.get("summary") or "Verifier accepted the task."),
            context=context_string(state_payload),
            iteration=str(state_payload["state"].get("seq", 0) + 1),
        )
        if proposed_tasks:
            state_payload["state"]["current_role"] = "planner"
            state_payload["state"]["current_task_id"] = ""
            state_payload["state"]["current_attempt"] = 0
            state_payload["state"]["replans"] += 1
            return {"decision": "relaunch", "reason": "planner_update_requested", "report": verdict, "tasks": tasks_payload}
        tasks_payload = refresh_ready_tasks(tasks_payload)
        if all_tasks_done(tasks_payload):
            state_payload["state"]["completed"] = True
            state_payload["state"]["current_role"] = ""
            state_payload["state"]["current_task_id"] = ""
            state_payload["state"]["current_attempt"] = 0
            return {"decision": "stop", "reason": "all_tasks_done", "report": verdict, "tasks": tasks_payload}
        next_task = next_ready_task(tasks_payload)
        if next_task is None:
            state_payload["state"]["current_role"] = "planner"
            state_payload["state"]["current_task_id"] = ""
            state_payload["state"]["current_attempt"] = 0
            return {"decision": "relaunch", "reason": "planner_replan_needed", "report": verdict, "tasks": tasks_payload}
        state_payload["state"]["current_role"] = "implementer"
        state_payload["state"]["current_task_id"] = str(next_task["id"])
        state_payload["state"]["current_attempt"] = int(next_task.get("attempts", 0)) + 1
        return {"decision": "relaunch", "reason": "dispatch_implementer", "report": verdict, "tasks": tasks_payload}

    if verdict_value == "needs_human":
        task["status"] = "blocked"
        task["blocked_reason"] = str(verdict.get("summary") or "Verifier escalated for human review.")
        state_payload["state"]["needs_human"] += 1
        state_payload["state"]["current_role"] = ""
        state_payload["state"]["current_task_id"] = ""
        state_payload["state"]["current_attempt"] = 0
        return {"decision": "needs_human", "reason": "verifier_escalated", "report": verdict, "tasks": tasks_payload}

    revert_trial_commit(paths.repo, trial_commit)
    state_payload["state"]["reverts"] += 1
    state_payload["state"]["trial_commit"] = ""
    task["last_verdict"] = "revert"
    max_attempts = int(state_payload["config"].get("max_task_attempts", 3))
    if int(task.get("attempts", 0)) >= max_attempts:
        task["status"] = "failed"
        task["blocked_reason"] = str(verdict.get("summary") or "Verifier rejected the task repeatedly.")
        state_payload["state"]["current_role"] = "planner"
        state_payload["state"]["current_task_id"] = ""
        state_payload["state"]["current_attempt"] = 0
        state_payload["state"]["replans"] += 1
        append_lesson(
            path=paths.lessons,
            title=f"Replan task {task_id}",
            category="planner",
            strategy=f"Task {task_id} exhausted {max_attempts} implementation attempts",
            outcome="replan",
            insight=str(verdict.get("summary") or "Repeated verifier rejection triggered replanning."),
            context=context_string(state_payload),
            iteration=str(state_payload["state"].get("seq", 0) + 1),
        )
        return {"decision": "relaunch", "reason": "planner_replan_after_revert", "report": verdict, "tasks": tasks_payload}

    task["status"] = "ready"
    if proposed_tasks:
        state_payload["state"]["current_role"] = "planner"
        state_payload["state"]["current_task_id"] = ""
        state_payload["state"]["current_attempt"] = 0
        state_payload["state"]["replans"] += 1
        return {"decision": "relaunch", "reason": "planner_update_requested", "report": verdict, "tasks": tasks_payload}
    state_payload["state"]["current_role"] = "implementer"
    state_payload["state"]["current_task_id"] = task_id
    state_payload["state"]["current_attempt"] = int(task.get("attempts", 0)) + 1
    return {"decision": "relaunch", "reason": "retry_task", "report": verdict, "tasks": tasks_payload}


def evaluate_supervisor_status(*, repo: str | Path | None = None, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = default_paths(repo)
    state_payload = read_json(paths.state)
    tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
    role = str(state_payload["state"].get("current_role") or "")
    seq = int(state_payload["state"].get("seq", 0)) + 1
    original_task_id = str(state_payload["state"].get("current_task_id") or "")
    original_attempt = int(state_payload["state"].get("current_attempt") or 0)
    original_trial_commit = str(state_payload["state"].get("trial_commit") or "")

    if role == "planner":
        outcome = planner_report_state(paths, state_payload, tasks_payload, report_override=report_override)
    elif role == "implementer":
        outcome = implementer_report_state(paths, state_payload, tasks_payload, report_override=report_override)
    elif role == "verifier":
        outcome = verifier_report_state(paths, state_payload, tasks_payload, report_override=report_override)
    else:
        raise HarnessError(f"Unsupported current role in state: {role!r}")

    tasks_payload = outcome["tasks"]
    write_tasks(paths.tasks, tasks_payload)

    report = outcome["report"]
    state_payload["state"]["last_decision"] = outcome["reason"]
    state_payload["state"]["seq"] = seq
    state_payload["updated_at"] = utc_now()
    write_json_atomic(paths.state, state_payload)
    event_task_id = original_task_id if role in {"implementer", "verifier"} else ""
    event_attempt = original_attempt if role in {"implementer", "verifier"} else 0
    event_commit = str(
        report.get("commit")
        or report.get("trial_commit")
        or report.get("evaluated_commit")
        or original_trial_commit
        or "-"
    )
    row = append_event(
        path=paths.events,
        seq=seq,
        role=role,
        task_id=event_task_id or "-",
        attempt=event_attempt,
        commit=event_commit,
        status=str(state_payload["state"].get("last_status") or "-"),
        decision=outcome["reason"],
        description=str(report.get("summary") or outcome["reason"]),
    )

    if outcome["decision"] == "stop":
        _append_summary_lesson(
            paths,
            state_payload,
            "summary",
            f"Accepted {state_payload['state'].get('accepts', 0)} tasks and reverted {state_payload['state'].get('reverts', 0)} trial commits.",
        )
    if outcome["decision"] == "needs_human":
        write_json_atomic(paths.state, state_payload)

    return {
        "decision": outcome["decision"],
        "reason": outcome["reason"],
        "seq": seq,
        "role": role,
        "task_id": original_task_id,
        "event": row,
        "current_role": state_payload["state"].get("current_role", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the post-turn harness transition logic.")
    parser.add_argument("--repo")
    args = parser.parse_args()
    print(json.dumps(evaluate_supervisor_status(repo=args.repo), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as exc:
        raise SystemExit(f"error: {exc}")
