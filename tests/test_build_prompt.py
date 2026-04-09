from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_artifacts import Paths  # noqa: E402
from harness_build_prompt import (  # noqa: E402
    build_implementer_prompt,
    build_planner_prompt,
    build_verifier_prompt,
)

FAKE_BASE = Path("/tmp/fake-repo")

FAKE_PATHS = Paths(
    repo=FAKE_BASE,
    launch=FAKE_BASE / "harness-launch.json",
    runtime=FAKE_BASE / "harness-runtime.json",
    runtime_log=FAKE_BASE / "harness-runtime.log",
    state=FAKE_BASE / "harness-state.json",
    events=FAKE_BASE / "harness-events.tsv",
    lessons=FAKE_BASE / "harness-lessons.md",
    plan=FAKE_BASE / "plan.md",
    tasks=FAKE_BASE / "tasks.json",
    reports=FAKE_BASE / "reports",
)

FAKE_STATE = {
    "version": 1,
    "config": {"goal": "Build something", "scope": "repo"},
    "state": {
        "active_tasks": {
            "T-001": {
                "role": "verifier",
                "attempt": 1,
                "trial_commit": "abc1234",
                "thread_id": "thread-1",
                "verifier_feedback": "",
            }
        },
        "planner_pending_reason": "",
    },
}

FAKE_TASKS = {
    "version": 1,
    "goal": "Build something",
    "planner_revision": 0,
    "tasks": [
        {
            "id": "T-001",
            "title": "First task",
            "description": "Do the first thing",
            "acceptance_criteria": ["It works"],
            "status": "ready",
            "priority": 1,
            "dependencies": [],
            "attempts": 0,
        }
    ],
    "created_at": "2025-01-01T00:00:00+00:00",
    "updated_at": "2025-01-01T00:00:00+00:00",
}


def _mock_read_json(path: Path) -> dict:
    if "state" in path.name:
        return FAKE_STATE
    if "tasks" in path.name:
        return FAKE_TASKS
    raise FileNotFoundError(path)


def _mock_load_tasks(path: Path) -> dict:
    return FAKE_TASKS


def _mock_refresh_ready_tasks(tasks: dict) -> dict:
    return tasks


def _build_planner_prompt_for(*, state: dict | None = None, tasks: dict | None = None) -> str:
    state_payload = deepcopy(state or FAKE_STATE)
    tasks_payload = deepcopy(tasks or FAKE_TASKS)

    def _read_json(path: Path) -> dict:
        if "state" in path.name:
            return deepcopy(state_payload)
        if "tasks" in path.name:
            return deepcopy(tasks_payload)
        raise FileNotFoundError(path)

    def _load_tasks(path: Path) -> dict:
        return deepcopy(tasks_payload)

    with (
        patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks),
        patch("harness_build_prompt.load_tasks", side_effect=_load_tasks),
        patch("harness_build_prompt.read_json", side_effect=_read_json),
    ):
        return build_planner_prompt(FAKE_PATHS)


class TestPlannerPrompt(unittest.TestCase):
    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_no_report_path_reference(self, _rj, _lt, _rr):
        prompt = build_planner_prompt(FAKE_PATHS)
        self.assertNotIn("reports/planner", prompt)

    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_has_structured_json_instruction(self, _rj, _lt, _rr):
        prompt = build_planner_prompt(FAKE_PATHS)
        self.assertIn("Return your report as structured JSON", prompt)

    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_keeps_field_list(self, _rj, _lt, _rr):
        prompt = build_planner_prompt(FAKE_PATHS)
        self.assertIn("role", prompt)
        self.assertIn("revision", prompt)
        self.assertIn("summary", prompt)
        self.assertIn("task_changes", prompt)

    def test_pending_integration_recovery_includes_structured_context(self) -> None:
        state = deepcopy(FAKE_STATE)
        state["state"]["recovery"] = {
            "status": "pending",
            "owner": "planner",
            "reason": "integration_conflict",
            "resume_role": "planner",
            "resume_task_id": "T-001",
            "resume_attempt": 2,
            "incident": {
                "owner": "planner",
                "reason": "integration_conflict",
                "resume_role": "planner",
                "resume_task_id": "T-001",
                "resume_attempt": 2,
                "commit": "deadbeef",
                "details": {"outcome": "conflict", "worktree_path": "/tmp/worktree-T-001"},
            },
            "retry": {
                "count": 0,
                "reason": "",
                "resume_role": "",
                "resume_task_id": "",
                "resume_attempt": 0,
            },
        }

        prompt = _build_planner_prompt_for(state=state)

        self.assertIn("Recovery owner: planner", prompt)
        self.assertIn("Recovery reason: integration_conflict", prompt)
        self.assertIn("Resume target: role=planner, task=T-001, attempt=2", prompt)
        self.assertIn("Incident details:", prompt)
        self.assertIn("- commit: deadbeef", prompt)
        self.assertIn('"outcome": "conflict"', prompt)
        self.assertIn("Create repair or sequencing tasks for semantic recovery", prompt)

    def test_pending_retry_recovery_includes_retry_details(self) -> None:
        state = deepcopy(FAKE_STATE)
        state["state"]["recovery"] = {
            "status": "pending",
            "owner": "runtime",
            "reason": "app_server_turn_failed",
            "resume_role": "implementer",
            "resume_task_id": "T-001",
            "resume_attempt": 3,
            "incident": {
                "owner": "",
                "reason": "",
                "resume_role": "",
                "resume_task_id": "",
                "resume_attempt": 0,
                "commit": "",
                "details": {},
            },
            "retry": {
                "count": 2,
                "reason": "app_server_turn_failed",
                "resume_role": "implementer",
                "resume_task_id": "T-001",
                "resume_attempt": 3,
            },
        }

        prompt = _build_planner_prompt_for(state=state)

        self.assertIn("Recovery owner: runtime", prompt)
        self.assertIn("Recovery reason: app_server_turn_failed", prompt)
        self.assertIn("Resume target: role=implementer, task=T-001, attempt=3", prompt)
        self.assertIn("Retry details:", prompt)
        self.assertIn("- count: 2", prompt)
        self.assertIn("- reason: app_server_turn_failed", prompt)

    def test_exhausted_retry_replan_includes_failed_task_snapshot(self) -> None:
        state = deepcopy(FAKE_STATE)
        state["state"]["planner_pending_reason"] = "planner_replan_after_revert"
        tasks = deepcopy(FAKE_TASKS)
        tasks["tasks"][0]["status"] = "failed"
        tasks["tasks"][0]["attempts"] = 3
        tasks["tasks"][0]["last_verdict"] = "revert"
        tasks["tasks"][0]["last_attempt_commit"] = "deadbeef"
        tasks["tasks"][0]["blocked_reason"] = "Attempt 3 rejected"

        prompt = _build_planner_prompt_for(state=state, tasks=tasks)

        self.assertIn("A task exhausted its implementation retries and needs planner repair.", prompt)
        self.assertIn("Planner requested reason: planner_replan_after_revert", prompt)
        self.assertIn(
            "- T-001: attempts=3, last_verdict=revert, last_attempt_commit=deadbeef, blocked_reason=Attempt 3 rejected",
            prompt,
        )
        self.assertIn("Replace, split, or sequence follow-up tasks", prompt)


class TestImplementerPrompt(unittest.TestCase):
    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_no_write_report_instruction(self, _rj, _lt, _rr):
        prompt = build_implementer_prompt(FAKE_PATHS)
        self.assertNotIn("Write the implementer report to", prompt)

    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_has_structured_json_instruction(self, _rj, _lt, _rr):
        prompt = build_implementer_prompt(FAKE_PATHS)
        self.assertIn("Return your report as structured JSON", prompt)

    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_keeps_field_list(self, _rj, _lt, _rr):
        prompt = build_implementer_prompt(FAKE_PATHS)
        self.assertIn("role", prompt)
        self.assertIn("task_id", prompt)
        self.assertIn("files_changed", prompt)


class TestVerifierPrompt(unittest.TestCase):
    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_no_write_report_instruction(self, _rj, _lt, _rr):
        prompt = build_verifier_prompt(FAKE_PATHS)
        self.assertNotIn("Write a verifier report to", prompt)

    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_has_structured_json_instruction(self, _rj, _lt, _rr):
        prompt = build_verifier_prompt(FAKE_PATHS)
        self.assertIn("Return your report as structured JSON", prompt)

    @patch("harness_build_prompt.refresh_ready_tasks", side_effect=_mock_refresh_ready_tasks)
    @patch("harness_build_prompt.load_tasks", side_effect=_mock_load_tasks)
    @patch("harness_build_prompt.read_json", side_effect=_mock_read_json)
    def test_keeps_field_list(self, _rj, _lt, _rr):
        prompt = build_verifier_prompt(FAKE_PATHS)
        self.assertIn("role", prompt)
        self.assertIn("verdict", prompt)
        self.assertIn("recovery_signal", prompt)
        self.assertIn("criteria_results", prompt)
        self.assertNotIn("needs_human", prompt)


if __name__ == "__main__":
    unittest.main()
