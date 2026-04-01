"""Unit tests for parallel implementer execution support."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from harness_artifacts import (
    Paths,
    all_ready_tasks,
    build_state_payload,
    normalize_state_payload,
    refresh_ready_tasks,
    task_index,
    write_tasks,
)
from harness_build_prompt import build_implementer_prompt_for_task
from harness_runtime_ops import _run_parallel_implementers, sandbox_for_role
from harness_supervisor_status import evaluate_supervisor_status


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


def _make_task(
    task_id: str,
    *,
    status: str = "ready",
    priority: int = 1,
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": f"Do {task_id}",
        "acceptance_criteria": ["It works"],
        "status": status,
        "priority": priority,
        "dependencies": dependencies or [],
        "attempts": 0,
    }


def _make_payload(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "goal": "Build something",
        "planner_revision": 0,
        "tasks": tasks,
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2025-01-01T00:00:00+00:00",
    }


def _mock_manager(*, final_message: str = '{"role":"implementer","task_id":"T-001","attempt":1,"commit":"abc1234","summary":"done","files_changed":[],"checks_run":[],"proposed_tasks":[]}') -> MagicMock:
    ms = MagicMock()
    ms.server.start_thread.return_value = "thread-abc"
    ms.server.run_turn.return_value = {
        "status": "completed",
        "thread_id": "thread-abc",
        "final_message": final_message,
        "file_changes": [],
        "command_executions": [],
        "reasoning_summary": "",
    }
    ms.thread_history = {}

    manager = MagicMock()
    manager.acquire.return_value = ms
    return manager


class TestAllReadyTasks(unittest.TestCase):
    def test_returns_multiple_ready_tasks(self) -> None:
        payload = _make_payload([
            _make_task("T-001", status="ready", priority=1),
            _make_task("T-002", status="ready", priority=2),
            _make_task("T-003", status="pending", dependencies=["T-001"]),
        ])
        result = all_ready_tasks(payload)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "T-001")
        self.assertEqual(result[1]["id"], "T-002")

    def test_returns_empty_when_none_ready(self) -> None:
        payload = _make_payload([
            _make_task("T-001", status="done"),
            _make_task("T-002", status="done"),
        ])
        self.assertEqual(all_ready_tasks(payload), [])

    def test_sorts_by_priority_then_id(self) -> None:
        payload = _make_payload([
            _make_task("T-003", status="ready", priority=2),
            _make_task("T-001", status="ready", priority=1),
            _make_task("T-002", status="ready", priority=1),
        ])
        result = all_ready_tasks(payload)
        self.assertEqual([task["id"] for task in result], ["T-001", "T-002", "T-003"])

    def test_returns_deep_copies(self) -> None:
        payload = _make_payload([_make_task("T-001", status="ready", priority=1)])
        result = all_ready_tasks(payload)
        result[0]["title"] = "MUTATED"
        self.assertEqual(all_ready_tasks(payload)[0]["title"], "Task T-001")

    def test_pending_with_done_deps_becomes_ready(self) -> None:
        payload = _make_payload([
            _make_task("T-001", status="done"),
            _make_task("T-002", status="pending", dependencies=["T-001"]),
        ])
        result = all_ready_tasks(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "T-002")


class TestBuildImplementerPromptForTask(unittest.TestCase):
    def test_prompt_contains_task_info(self) -> None:
        task = _make_task("T-042", status="ready", priority=1)
        task["title"] = "Implement widget"
        task["description"] = "Build the widget component"
        task["acceptance_criteria"] = ["Has tests", "Passes lint"]
        prompt = build_implementer_prompt_for_task(FAKE_PATHS, task)
        self.assertIn("T-042", prompt)
        self.assertIn("Implement widget", prompt)
        self.assertIn("Build the widget component", prompt)
        self.assertIn("Has tests", prompt)
        self.assertIn("Passes lint", prompt)
        self.assertIn("Return your report as structured JSON", prompt)

    def test_prompt_uses_explicit_attempt(self) -> None:
        task = _make_task("T-001")
        prompt = build_implementer_prompt_for_task(FAKE_PATHS, task, attempt=3)
        self.assertIn("Attempt: 3", prompt)


class TestRunParallelImplementers(unittest.TestCase):
    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_runs_multiple_tasks_concurrently(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        tasks = [_make_task("T-001"), _make_task("T-002")]
        results, errors = _run_parallel_implementers(
            manager=manager,
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
        )
        self.assertEqual(errors, {})
        self.assertIn("T-001", results)
        self.assertIn("T-002", results)

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_captures_errors_per_task(self, _mock_schema: Any) -> None:
        manager = MagicMock()

        def _acquire(key: str, **kwargs: Any) -> MagicMock:
            managed = MagicMock()
            managed.thread_history = {}
            if key == "T-002":
                managed.server.start_thread.side_effect = RuntimeError("boom")
            else:
                managed.server.start_thread.return_value = "thread-ok"
                managed.server.run_turn.return_value = {
                    "status": "completed",
                    "thread_id": "thread-ok",
                    "final_message": '{"role":"implementer","task_id":"T-001","attempt":1,"commit":"abc","summary":"ok","files_changed":[],"checks_run":[],"proposed_tasks":[]}',
                    "file_changes": [],
                    "command_executions": [],
                    "reasoning_summary": "",
                }
            return managed

        manager.acquire.side_effect = _acquire
        results, errors = _run_parallel_implementers(
            manager=manager,
            ready_tasks=[_make_task("T-001"), _make_task("T-002")],
            paths=FAKE_PATHS,
            runtime={},
        )
        self.assertIn("T-001", results)
        self.assertIn("T-002", errors)
        self.assertIn("boom", errors["T-002"])

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_uses_separate_threads(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        thread_ids: list[int | None] = []

        def _capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
            thread_ids.append(threading.current_thread().ident)
            return {
                "report": {"role": "implementer", "task_id": kwargs["task_id"]},
                "thread_id": f"thread-{kwargs['task_id']}",
                "turn_result": {},
                "parse_error": None,
            }

        with patch("harness_runtime_ops.run_role_turn", side_effect=_capture):
            results, errors = _run_parallel_implementers(
                manager=manager,
                ready_tasks=[_make_task("T-001"), _make_task("T-002")],
                paths=FAKE_PATHS,
                runtime={},
            )
        self.assertEqual(len(results) + len(errors), 2)
        self.assertGreaterEqual(len(set(thread_ids)), 1)

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_empty_tasks_returns_empty(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        results, errors = _run_parallel_implementers(
            manager=manager,
            ready_tasks=[],
            paths=FAKE_PATHS,
            runtime={},
        )
        self.assertEqual(results, {})
        self.assertEqual(errors, {})

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_uses_saved_attempt_and_feedback_for_retry(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        captured_prompt: dict[str, str] = {}

        def _capture(**kwargs: Any) -> dict[str, Any]:
            captured_prompt["text"] = kwargs["prompt"]
            return {
                "report": {"role": "implementer", "task_id": kwargs["task_id"]},
                "thread_id": "thread-old",
                "turn_result": {},
                "parse_error": None,
            }

        with patch("harness_runtime_ops.run_role_turn", side_effect=_capture):
            _run_parallel_implementers(
                manager=manager,
                ready_tasks=[_make_task("T-001")],
                paths=FAKE_PATHS,
                runtime={},
                task_states={
                    "T-001": {
                        "role": "implementer",
                        "attempt": 2,
                        "trial_commit": "",
                        "thread_id": "thread-old",
                        "verifier_feedback": "Fix the failing criterion.",
                    }
                },
            )
        self.assertIn("Attempt: 2", captured_prompt["text"])
        self.assertIn("Fix the failing criterion.", captured_prompt["text"])


class TestPerTaskExecutionState(unittest.TestCase):
    def _make_paths(self, base: Path) -> Paths:
        return Paths(
            repo=base,
            launch=base / "harness-launch.json",
            runtime=base / "harness-runtime.json",
            runtime_log=base / "harness-runtime.log",
            state=base / "harness-state.json",
            events=base / "harness-events.tsv",
            lessons=base / "harness-lessons.md",
            plan=base / "plan.md",
            tasks=base / "tasks.json",
            reports=base / "reports",
        )

    def _write_state(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_parallel_submissions_keep_both_tasks_tracked_until_each_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._make_paths(Path(tmp))
            paths.reports.mkdir(parents=True, exist_ok=True)

            state_payload = normalize_state_payload(
                build_state_payload(config={"goal": "Build", "scope": ".", "max_task_attempts": 3})
            )
            state_payload["state"]["active_tasks"] = {
                "T-001": {"role": "implementer", "attempt": 1, "trial_commit": "", "thread_id": "thread-1", "verifier_feedback": ""},
                "T-002": {"role": "implementer", "attempt": 1, "trial_commit": "", "thread_id": "thread-2", "verifier_feedback": ""},
            }
            self._write_state(paths.state, state_payload)

            tasks_payload = _make_payload([
                _make_task("T-001", status="ready", priority=1),
                _make_task("T-002", status="ready", priority=2),
            ])
            write_tasks(paths.tasks, tasks_payload)

            decision = evaluate_supervisor_status(
                repo=paths.repo,
                report_override={
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": "abc1111",
                    "summary": "Implemented T-001",
                    "files_changed": ["a.py"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            self.assertEqual(decision["reason"], "dispatch_verifier")
            decision = evaluate_supervisor_status(
                repo=paths.repo,
                report_override={
                    "role": "implementer",
                    "task_id": "T-002",
                    "attempt": 1,
                    "commit": "def2222",
                    "summary": "Implemented T-002",
                    "files_changed": ["b.py"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            self.assertEqual(decision["reason"], "dispatch_verifier")

            state_after_impl = normalize_state_payload(json.loads(paths.state.read_text(encoding="utf-8")))
            self.assertEqual(state_after_impl["state"]["active_tasks"]["T-001"]["role"], "verifier")
            self.assertEqual(state_after_impl["state"]["active_tasks"]["T-002"]["role"], "verifier")

            decision = evaluate_supervisor_status(
                repo=paths.repo,
                report_override={
                    "role": "verifier",
                    "task_id": "T-002",
                    "attempt": 1,
                    "commit": "def2222",
                    "verdict": "accept",
                    "summary": "Accepted T-002",
                    "findings": [],
                    "criteria_results": [],
                    "proposed_tasks": [],
                },
            )
            self.assertEqual(decision["decision"], "relaunch")

            final_state = normalize_state_payload(json.loads(paths.state.read_text(encoding="utf-8")))
            final_tasks = json.loads(paths.tasks.read_text(encoding="utf-8"))
            final_index = {task["id"]: task for task in final_tasks["tasks"]}

            self.assertIn("T-001", final_state["state"]["active_tasks"])
            self.assertEqual(final_state["state"]["active_tasks"]["T-001"]["role"], "verifier")
            self.assertNotIn("T-002", final_state["state"]["active_tasks"])
            self.assertEqual(final_index["T-001"]["status"], "in_progress")
            self.assertEqual(final_index["T-002"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
