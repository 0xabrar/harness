from __future__ import annotations

import json
import sys
import unittest
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
        "current_role": "planner",
        "current_task_id": "T-001",
        "current_attempt": 1,
        "trial_commit": "abc1234",
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
        self.assertIn("criteria_results", prompt)


if __name__ == "__main__":
    unittest.main()
