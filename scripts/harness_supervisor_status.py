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
    all_ready_tasks,
    default_recovery_payload,
    default_paths,
    load_tasks,
    normalize_state_payload,
    read_json,
    refresh_ready_tasks,
    report_path_for_role,
    task_index,
    utc_now,
    write_json_atomic,
    write_tasks,
)
from harness_lessons import append_lesson
from harness_task_worktree import git_head, integrate_commit, remove_task_worktree, reset_task_worktree

VERIFIER_RECOVERY_SIGNALS = {
    "none",
    "environment_blocked",
    "ambiguous_acceptance_criteria",
}


def _active_tasks(state_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return normalize_state_payload(state_payload)["state"]["active_tasks"]


def _clear_recovery(state_payload: dict[str, Any]) -> None:
    normalize_state_payload(state_payload)["state"]["recovery"] = default_recovery_payload()


def _set_recovery(
    state_payload: dict[str, Any],
    *,
    owner: str,
    reason: str,
    resume_role: str = "",
    resume_task_id: str = "",
    resume_attempt: int = 0,
    commit: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    recovery = default_recovery_payload()
    recovery.update(
        {
            "status": "pending",
            "owner": owner,
            "reason": reason,
            "resume_role": resume_role,
            "resume_task_id": resume_task_id,
            "resume_attempt": resume_attempt,
        }
    )
    recovery["incident"].update(
        {
            "owner": owner,
            "reason": reason,
            "resume_role": resume_role,
            "resume_task_id": resume_task_id,
            "resume_attempt": resume_attempt,
            "commit": commit,
            "details": dict(details or {}),
        }
    )
    normalize_state_payload(state_payload)["state"]["recovery"] = recovery


def _integration_recovery_details(record: dict[str, Any], *, outcome: str, detail: str, returncode: int) -> dict[str, Any]:
    details: dict[str, Any] = {
        "outcome": outcome,
        "returncode": returncode,
    }
    if detail:
        details["detail"] = detail
    for key in ("branch_name", "worktree_path", "base_commit"):
        value = str(record.get(key) or "")
        if value:
            details[key] = value
    return details


def _repair_target_task_id(task: dict[str, Any]) -> str:
    for key in ("repair_target_task_id", "repair_for_task_id", "repair_target"):
        value = str(task.get(key) or "")
        if value:
            return value
    return ""


def _finalize_repair_target(
    paths,
    tasks_payload: dict[str, Any],
    *,
    repair_task: dict[str, Any],
    integrated_commit: str,
) -> None:
    repair_task_id = str(repair_task["id"])
    target_id = _repair_target_task_id(repair_task)
    if not target_id or target_id == repair_task_id:
        return

    index = task_index(tasks_payload)
    target = index.get(target_id)
    if target is None:
        raise HarnessError(
            f"Repair task {repair_task_id!r} references missing repair target {target_id!r}."
        )

    conflict_details = dict(target.get("integration_conflict") or {})
    if not conflict_details:
        return

    remove_task_worktree(
        repo=paths.repo,
        branch_name=str(conflict_details.get("branch_name") or ""),
        worktree_path=str(conflict_details.get("worktree_path") or ""),
    )
    target.pop("integration_conflict", None)

    target["status"] = "done"
    target["last_verdict"] = "accept"
    target["last_integrated_commit"] = integrated_commit
    target["resolved_by_task_id"] = repair_task_id
    target.pop("blocked_reason", None)


def _task_sort_key(task: dict[str, Any]) -> tuple[int, str]:
    return (int(task.get("priority", 100)), str(task["id"]))


def _sorted_active_task_ids(tasks_payload: dict[str, Any], state_payload: dict[str, Any], *, role: str) -> list[str]:
    active = _active_tasks(state_payload)
    index = task_index(tasks_payload)
    ids = [task_id for task_id, record in active.items() if str(record.get("role") or "") == role]
    ids.sort(key=lambda task_id: _task_sort_key(index.get(task_id, {"id": task_id, "priority": 100})))
    return ids


def _ready_tasks_not_active(tasks_payload: dict[str, Any], state_payload: dict[str, Any]) -> list[dict[str, Any]]:
    active = _active_tasks(state_payload)
    return [task for task in all_ready_tasks(tasks_payload) if str(task["id"]) not in active]


def _active_role_summary(tasks_payload: dict[str, Any], state_payload: dict[str, Any]) -> str:
    if _sorted_active_task_ids(tasks_payload, state_payload, role="verifier"):
        return "verifier"
    if _sorted_active_task_ids(tasks_payload, state_payload, role="implementer"):
        return "implementer"
    if str(state_payload["state"].get("planner_pending_reason") or ""):
        return "planner"
    return ""


def context_string(state_payload: dict[str, Any], tasks_payload: dict[str, Any] | None = None) -> str:
    config = state_payload.get("config", {})
    role = ""
    if tasks_payload is not None:
        role = _active_role_summary(tasks_payload, state_payload)
    return (
        f"goal={config.get('goal', '')}; "
        f"scope={config.get('scope', '')}; "
        f"role={role}"
    )


def _append_summary_lesson(paths, state_payload: dict[str, Any], tasks_payload: dict[str, Any], outcome: str, insight: str) -> None:
    append_lesson(
        path=paths.lessons,
        title=f"Run summary: {state_payload['config'].get('goal', 'Harness run')}",
        category="summary",
        strategy="Runtime completion summary",
        outcome=outcome,
        insight=insight,
        context=context_string(state_payload, tasks_payload),
        iteration=str(state_payload["state"].get("seq", 0)),
    )


def revert_trial_commit(repo: Path, trial_commit: str) -> None:
    completed = subprocess.run(["git", "-C", str(repo), "revert", "--no-edit", trial_commit], text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise HarnessError(completed.stderr.strip() or f"Failed to revert commit {trial_commit}")


def _detect_role(state_payload: dict[str, Any], tasks_payload: dict[str, Any], report_override: dict[str, Any] | None) -> str:
    if report_override is not None:
        report_role = str(report_override.get("role") or "")
        if report_role:
            return report_role
    if _sorted_active_task_ids(tasks_payload, state_payload, role="verifier"):
        return "verifier"
    if _sorted_active_task_ids(tasks_payload, state_payload, role="implementer"):
        return "implementer"
    return "planner"


def planner_report_state(paths, state_payload: dict[str, Any], tasks_payload: dict[str, Any], *, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    current_revision = int(state_payload["state"].get("planner_revision", 0))
    report_path = report_path_for_role(paths, "planner", planner_revision=current_revision + 1)
    if report_override is not None:
        report = report_override
        write_json_atomic(report_path, report)
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
    state_payload["state"]["planner_pending_reason"] = ""
    state_payload["state"]["last_status"] = "plan"

    tasks_payload = refresh_ready_tasks(tasks_payload)
    if all_tasks_done(tasks_payload):
        state_payload["state"]["completed"] = True
        _clear_recovery(state_payload)
        return {"decision": "stop", "reason": "all_tasks_done", "report": report, "tasks": tasks_payload}

    ready_tasks = _ready_tasks_not_active(tasks_payload, state_payload)
    if not ready_tasks:
        state_payload["state"]["last_status"] = "recovery"
        state_payload["state"]["recovery_requests"] += 1
        _set_recovery(
            state_payload,
            owner="planner",
            reason="planner_left_no_ready_tasks",
            resume_role="planner",
        )
        return {
            "decision": "recovery",
            "reason": "planner_left_no_ready_tasks",
            "report": report,
            "tasks": tasks_payload,
        }

    _clear_recovery(state_payload)
    active = _active_tasks(state_payload)
    for task in ready_tasks:
        active[str(task["id"])] = {
            "role": "implementer",
            "attempt": int(task.get("attempts", 0)) + 1,
            "trial_commit": "",
            "thread_id": str(active.get(str(task["id"]), {}).get("thread_id") or ""),
            "verifier_feedback": str(active.get(str(task["id"]), {}).get("verifier_feedback") or ""),
            "branch_name": str(active.get(str(task["id"]), {}).get("branch_name") or ""),
            "worktree_path": str(active.get(str(task["id"]), {}).get("worktree_path") or ""),
            "base_commit": str(active.get(str(task["id"]), {}).get("base_commit") or ""),
        }
    return {"decision": "relaunch", "reason": "dispatch_implementer", "report": report, "tasks": tasks_payload}


def implementer_report_state(paths, state_payload: dict[str, Any], tasks_payload: dict[str, Any], *, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    role_ids = _sorted_active_task_ids(tasks_payload, state_payload, role="implementer")
    report = report_override
    if report is None:
        if len(role_ids) != 1:
            raise HarnessError("Implementer state is ambiguous without an explicit report.")
        task_id = role_ids[0]
        record = _active_tasks(state_payload)[task_id]
        attempt = int(record.get("attempt") or 0)
        report_path = report_path_for_role(paths, "implementer", task_id=task_id, attempt=attempt)
        if not report_path.exists():
            raise HarnessError(f"Implementer report missing: {report_path}")
        report = read_json(report_path)

    task_id = str(report.get("task_id") or (role_ids[0] if len(role_ids) == 1 else ""))
    if not task_id:
        raise HarnessError("Implementer report is missing task_id.")
    record = _active_tasks(state_payload).get(task_id)
    if record is None:
        raise HarnessError(f"Implementer state missing active task {task_id}.")
    attempt = int(report.get("attempt") or record.get("attempt") or 0)
    if not attempt:
        raise HarnessError("Implementer state is missing current task attempt.")

    report_path = report_path_for_role(paths, "implementer", task_id=task_id, attempt=attempt)
    write_json_atomic(report_path, report)

    commit = str(report.get("commit") or report.get("trial_commit") or "")
    if not commit:
        raise HarnessError("Implementer report must include a commit.")

    index = task_index(tasks_payload)
    task = index[task_id]
    task["status"] = "in_progress"
    task["attempts"] = attempt
    task["last_attempt_commit"] = commit

    _active_tasks(state_payload)[task_id] = {
        "role": "verifier",
        "attempt": attempt,
        "trial_commit": commit,
        "thread_id": str(record.get("thread_id") or ""),
        "verifier_feedback": "",
        "branch_name": str(record.get("branch_name") or ""),
        "worktree_path": str(record.get("worktree_path") or ""),
        "base_commit": str(record.get("base_commit") or ""),
    }

    state_payload["state"]["implementer_runs"] += 1
    state_payload["state"]["last_status"] = "submit_trial"
    _clear_recovery(state_payload)
    return {"decision": "relaunch", "reason": "dispatch_verifier", "report": report, "tasks": tasks_payload}


def verifier_report_state(paths, state_payload: dict[str, Any], tasks_payload: dict[str, Any], *, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    role_ids = _sorted_active_task_ids(tasks_payload, state_payload, role="verifier")
    report = report_override
    if report is None:
        if len(role_ids) != 1:
            raise HarnessError("Verifier state is ambiguous without an explicit report.")
        task_id = role_ids[0]
        record = _active_tasks(state_payload)[task_id]
        attempt = int(record.get("attempt") or 0)
        verdict_path = report_path_for_role(paths, "verifier", task_id=task_id, attempt=attempt)
        if not verdict_path.exists():
            raise HarnessError(f"Verifier report missing: {verdict_path}")
        report = read_json(verdict_path)

    task_id = str(report.get("task_id") or (role_ids[0] if len(role_ids) == 1 else ""))
    if not task_id:
        raise HarnessError("Verifier report is missing task_id.")
    active = _active_tasks(state_payload)
    record = active.get(task_id)
    if record is None:
        raise HarnessError(f"Active verifier state missing for task {task_id}.")

    attempt = int(report.get("attempt") or record.get("attempt") or 0)
    commit = str(report.get("commit") or report.get("evaluated_commit") or report.get("trial_commit") or record.get("trial_commit") or "")
    if not commit:
        raise HarnessError(f"Verifier report for {task_id} must identify the evaluated commit.")

    verdict = str(report.get("verdict") or "")
    if verdict not in {"accept", "revert"}:
        raise HarnessError("Verifier verdict must be accept or revert.")
    recovery_signal = str(report.get("recovery_signal") or "none")
    if recovery_signal not in VERIFIER_RECOVERY_SIGNALS:
        raise HarnessError(
            "Verifier recovery_signal must be none, environment_blocked, or ambiguous_acceptance_criteria."
        )
    if recovery_signal != "none" and verdict != "revert":
        raise HarnessError("Verifier recovery_signal requires verdict=revert.")

    verdict_path = report_path_for_role(paths, "verifier", task_id=task_id, attempt=attempt)
    write_json_atomic(verdict_path, report)

    implementer = read_json(report_path_for_role(paths, "implementer", task_id=task_id, attempt=attempt))
    proposed_tasks = list(implementer.get("proposed_tasks") or []) + list(report.get("proposed_tasks") or [])

    index = task_index(tasks_payload)
    task = index[task_id]
    summary = str(report.get("summary") or f"Verifier returned {verdict}.")

    state_payload["state"]["verifier_runs"] += 1
    state_payload["state"]["last_verdict"] = verdict

    if recovery_signal != "none":
        state_payload["state"]["last_status"] = "recovery"
        task["status"] = "blocked"
        task["blocked_reason"] = summary
        task["last_verdict"] = "revert"
        state_payload["state"]["recovery_requests"] += 1
        state_payload["state"]["blocked"] += 1
        active.pop(task_id, None)
        _set_recovery(
            state_payload,
            owner="planner",
            reason=recovery_signal,
            resume_role="planner",
            resume_task_id=task_id,
            resume_attempt=attempt,
            commit=commit,
        )
        return {"decision": "recovery", "reason": recovery_signal, "report": report, "tasks": tasks_payload}

    if verdict == "accept":
        integrated_commit = commit
        if str(record.get("worktree_path") or ""):
            integration = integrate_commit(repo=paths.repo, commit=commit)
            if integration.outcome != "applied":
                incident_reason = f"integration_{integration.outcome}"
                conflict_details = _integration_recovery_details(
                    record,
                    outcome=integration.outcome,
                    detail=integration.detail,
                    returncode=integration.returncode,
                )
                task["integration_conflict"] = {
                    "attempt": attempt,
                    "commit": commit,
                    **conflict_details,
                }
                task["status"] = "blocked"
                task["blocked_reason"] = (
                    f"Accepted task could not be integrated onto main ({integration.outcome})."
                )
                task["last_verdict"] = "accept"
                state_payload["state"]["last_status"] = "recovery"
                state_payload["state"]["recovery_requests"] += 1
                state_payload["state"]["blocked"] += 1
                active.pop(task_id, None)
                _set_recovery(
                    state_payload,
                    owner="planner",
                    reason=incident_reason,
                    resume_role="planner",
                    resume_task_id=task_id,
                    resume_attempt=attempt,
                    commit=commit,
                    details=conflict_details,
                )
                return {
                    "decision": "recovery",
                    "reason": incident_reason,
                    "report": report,
                    "tasks": tasks_payload,
                }
            integrated_commit = integration.integrated_commit
        task.pop("integration_conflict", None)
        _finalize_repair_target(paths, tasks_payload, repair_task=task, integrated_commit=integrated_commit)
        _clear_recovery(state_payload)
        state_payload["state"]["last_status"] = "accept"
        task["status"] = "done"
        task["last_verdict"] = "accept"
        task["last_integrated_commit"] = integrated_commit
        state_payload["state"]["accepts"] += 1
        state_payload["state"]["accepted_commit"] = integrated_commit
        active.pop(task_id, None)
        if str(record.get("worktree_path") or "") or str(record.get("branch_name") or ""):
            remove_task_worktree(
                repo=paths.repo,
                branch_name=str(record.get("branch_name") or ""),
                worktree_path=str(record.get("worktree_path") or ""),
            )
        append_lesson(
            path=paths.lessons,
            title=f"Accepted task {task_id}",
            category="task",
            strategy=str(implementer.get("summary") or f"Completed {task_id}"),
            outcome="accept",
            insight=summary,
            context=context_string(state_payload, tasks_payload),
            iteration=str(int(state_payload["state"].get("seq", 0)) + 1),
        )
        if proposed_tasks:
            state_payload["state"]["planner_pending_reason"] = "planner_update_requested"

        tasks_payload = refresh_ready_tasks(tasks_payload)
        if all_tasks_done(tasks_payload):
            state_payload["state"]["completed"] = True
            return {"decision": "stop", "reason": "all_tasks_done", "report": report, "tasks": tasks_payload}
        if not _active_tasks(state_payload) and not _ready_tasks_not_active(tasks_payload, state_payload):
            state_payload["state"]["planner_pending_reason"] = state_payload["state"].get("planner_pending_reason") or "planner_replan_needed"
            return {
                "decision": "relaunch",
                "reason": str(state_payload["state"]["planner_pending_reason"]),
                "report": report,
                "tasks": tasks_payload,
            }
        return {"decision": "relaunch", "reason": "continue_after_accept", "report": report, "tasks": tasks_payload}

    _clear_recovery(state_payload)
    state_payload["state"]["last_status"] = "revert"
    if str(record.get("worktree_path") or ""):
        next_base = git_head(paths.repo)
        reset_task_worktree(
            repo=paths.repo,
            worktree_path=str(record.get("worktree_path") or ""),
            base_commit=next_base,
        )
        record["base_commit"] = next_base
    else:
        revert_trial_commit(paths.repo, commit)
    state_payload["state"]["reverts"] += 1
    task["last_verdict"] = "revert"
    append_lesson(
        path=paths.lessons,
        title=f"Reverted task {task_id} attempt {attempt}",
        category="task",
        strategy=str(implementer.get("summary") or f"Attempt {attempt} for {task_id}"),
        outcome="revert",
        insight=summary,
        context=context_string(state_payload, tasks_payload),
        iteration=str(int(state_payload["state"].get("seq", 0)) + 1),
    )

    max_attempts = int(state_payload["config"].get("max_task_attempts", 3))
    if int(task.get("attempts", 0)) >= max_attempts:
        task["status"] = "failed"
        task["blocked_reason"] = summary
        active.pop(task_id, None)
        state_payload["state"]["replans"] += 1
        state_payload["state"]["planner_pending_reason"] = "planner_replan_after_revert"
        append_lesson(
            path=paths.lessons,
            title=f"Replan task {task_id}",
            category="planner",
            strategy=f"Task {task_id} exhausted {max_attempts} implementation attempts",
            outcome="replan",
            insight=summary,
            context=context_string(state_payload, tasks_payload),
            iteration=str(int(state_payload["state"].get("seq", 0)) + 1),
        )
        return {"decision": "relaunch", "reason": "planner_replan_after_revert", "report": report, "tasks": tasks_payload}

    if proposed_tasks:
        task["status"] = "ready"
        active.pop(task_id, None)
        state_payload["state"]["replans"] += 1
        state_payload["state"]["planner_pending_reason"] = "planner_update_requested"
        return {"decision": "relaunch", "reason": "planner_update_requested", "report": report, "tasks": tasks_payload}

    task["status"] = "ready"
    active[task_id] = {
        "role": "implementer",
        "attempt": attempt + 1,
        "trial_commit": "",
        "thread_id": str(record.get("thread_id") or ""),
        "verifier_feedback": summary,
        "branch_name": str(record.get("branch_name") or ""),
        "worktree_path": str(record.get("worktree_path") or ""),
        "base_commit": str(record.get("base_commit") or ""),
    }
    return {"decision": "relaunch", "reason": "retry_task", "report": report, "tasks": tasks_payload}


def evaluate_supervisor_status(*, repo: str | Path | None = None, report_override: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = default_paths(repo)
    state_payload = normalize_state_payload(read_json(paths.state))
    tasks_payload = refresh_ready_tasks(load_tasks(paths.tasks))
    role = _detect_role(state_payload, tasks_payload, report_override)
    seq = int(state_payload["state"].get("seq", 0)) + 1

    if role == "planner":
        outcome = planner_report_state(paths, state_payload, tasks_payload, report_override=report_override)
        event_task_id = ""
        event_attempt = 0
    elif role == "implementer":
        outcome = implementer_report_state(paths, state_payload, tasks_payload, report_override=report_override)
        event_task_id = str(outcome["report"].get("task_id") or "")
        event_attempt = int(outcome["report"].get("attempt") or 0)
    elif role == "verifier":
        outcome = verifier_report_state(paths, state_payload, tasks_payload, report_override=report_override)
        event_task_id = str(outcome["report"].get("task_id") or "")
        event_attempt = int(outcome["report"].get("attempt") or 0)
    else:
        raise HarnessError(f"Unsupported role: {role!r}")

    tasks_payload = outcome["tasks"]
    write_tasks(paths.tasks, tasks_payload)

    report = outcome["report"]
    state_payload["state"]["last_decision"] = outcome["reason"]
    state_payload["state"]["seq"] = seq
    state_payload["updated_at"] = utc_now()
    write_json_atomic(paths.state, state_payload)
    event_commit = str(
        report.get("commit")
        or report.get("trial_commit")
        or report.get("evaluated_commit")
        or _active_tasks(state_payload).get(event_task_id, {}).get("trial_commit")
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
            tasks_payload,
            "summary",
            f"Accepted {state_payload['state'].get('accepts', 0)} tasks and reverted {state_payload['state'].get('reverts', 0)} trial commits.",
        )

    return {
        "decision": outcome["decision"],
        "reason": outcome["reason"],
        "seq": seq,
        "role": role,
        "task_id": event_task_id,
        "event": row,
        "active_role": _active_role_summary(tasks_payload, state_payload),
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
