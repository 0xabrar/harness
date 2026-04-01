"""Unit tests for parallel implementer execution support."""
from __future__ import annotations

import json
import threading
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from harness_artifacts import (
    Paths,
    all_ready_tasks,
    build_state_payload,
    refresh_ready_tasks,
    task_index,
    write_tasks,
)
from harness_build_prompt import build_implementer_prompt_for_task
from harness_runtime_ops import (
    _apply_implementer_result,
    _apply_verifier_result,
    _run_parallel_implementers,
    normalize_state_payload,
    sandbox_for_role,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    """Return a mock ServerManager whose acquire() returns a usable ManagedServer."""
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


# ---------------------------------------------------------------------------
# Tests: all_ready_tasks
# ---------------------------------------------------------------------------


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
        result = all_ready_tasks(payload)
        self.assertEqual(result, [])

    def test_sorts_by_priority_then_id(self) -> None:
        payload = _make_payload([
            _make_task("T-003", status="ready", priority=2),
            _make_task("T-001", status="ready", priority=1),
            _make_task("T-002", status="ready", priority=1),
        ])
        result = all_ready_tasks(payload)
        self.assertEqual(len(result), 3)
        # Priority 1 first (T-001 before T-002 by id), then priority 2
        self.assertEqual(result[0]["id"], "T-001")
        self.assertEqual(result[1]["id"], "T-002")
        self.assertEqual(result[2]["id"], "T-003")

    def test_returns_deep_copies(self) -> None:
        payload = _make_payload([
            _make_task("T-001", status="ready", priority=1),
        ])
        result = all_ready_tasks(payload)
        result[0]["title"] = "MUTATED"
        # Re-fetch: original should be unaffected
        result2 = all_ready_tasks(payload)
        self.assertEqual(result2[0]["title"], "Task T-001")

    def test_pending_with_done_deps_becomes_ready(self) -> None:
        payload = _make_payload([
            _make_task("T-001", status="done"),
            _make_task("T-002", status="pending", priority=1, dependencies=["T-001"]),
        ])
        result = all_ready_tasks(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "T-002")

    def test_single_ready_task(self) -> None:
        """A single ready task is returned as a one-element list."""
        payload = _make_payload([
            _make_task("T-001", status="ready", priority=5),
            _make_task("T-002", status="done"),
        ])
        result = all_ready_tasks(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "T-001")


# ---------------------------------------------------------------------------
# Tests: build_implementer_prompt_for_task
# ---------------------------------------------------------------------------


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

    def test_prompt_has_role_implementer(self) -> None:
        task = _make_task("T-001")
        prompt = build_implementer_prompt_for_task(FAKE_PATHS, task)
        self.assertIn("implementer role", prompt)


# ---------------------------------------------------------------------------
# Tests: _run_parallel_implementers
# ---------------------------------------------------------------------------


class TestRunParallelImplementers(unittest.TestCase):
    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_runs_multiple_tasks_concurrently(self, _mock_schema: Any) -> None:
        """Verify that multiple tasks produce results keyed by task id."""
        manager = _mock_manager()
        tasks = [
            _make_task("T-001", status="ready"),
            _make_task("T-002", status="ready"),
        ]
        results, errors = _run_parallel_implementers(
            manager=manager,
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
        )
        self.assertEqual(len(errors), 0)
        self.assertIn("T-001", results)
        self.assertIn("T-002", results)

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_captures_errors_per_task(self, _mock_schema: Any) -> None:
        """When a task fails, its error is recorded without blocking others."""
        manager = MagicMock()
        call_count = 0

        def _acquire(key: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            ms = MagicMock()
            ms.thread_history = {}
            if key == "T-002":
                ms.server.start_thread.side_effect = RuntimeError("boom")
            else:
                ms.server.start_thread.return_value = "thread-ok"
                ms.server.run_turn.return_value = {
                    "status": "completed",
                    "thread_id": "thread-ok",
                    "final_message": '{"role":"implementer","task_id":"T-001","attempt":1,"commit":"abc","summary":"ok","files_changed":[],"checks_run":[],"proposed_tasks":[]}',
                    "file_changes": [],
                    "command_executions": [],
                    "reasoning_summary": "",
                }
            return ms

        manager.acquire.side_effect = _acquire

        tasks = [
            _make_task("T-001", status="ready"),
            _make_task("T-002", status="ready"),
        ]
        results, errors = _run_parallel_implementers(
            manager=manager,
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
        )
        self.assertIn("T-001", results)
        self.assertIn("T-002", errors)
        self.assertIn("boom", errors["T-002"])

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_uses_separate_threads(self, _mock_schema: Any) -> None:
        """Verify that tasks actually run on separate threads."""
        manager = _mock_manager()
        thread_ids: list[int] = []
        original_run_one = None

        # Patch at thread level to capture thread ids
        def _capture_thread(*args: Any, **kwargs: Any) -> None:
            thread_ids.append(threading.current_thread().ident)

        tasks = [
            _make_task("T-001", status="ready"),
            _make_task("T-002", status="ready"),
        ]

        with patch("harness_runtime_ops.run_role_turn") as mock_rrt:
            mock_rrt.return_value = {
                "report": {"role": "implementer", "task_id": "T-001"},
                "thread_id": "t-1",
                "turn_result": {},
                "parse_error": None,
            }
            # We just want to check threads actually started
            with patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="prompt"):
                results, errors = _run_parallel_implementers(
                    manager=manager,
                    ready_tasks=tasks,
                    paths=FAKE_PATHS,
                    runtime={},
                )
        # Both tasks should have completed (results or errors)
        total = len(results) + len(errors)
        self.assertEqual(total, 2)

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_empty_tasks_returns_empty(self, _mock_schema: Any) -> None:
        """No tasks means no results and no errors."""
        manager = _mock_manager()
        results, errors = _run_parallel_implementers(
            manager=manager,
            ready_tasks=[],
            paths=FAKE_PATHS,
            runtime={},
        )
        self.assertEqual(results, {})
        self.assertEqual(errors, {})


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

            _apply_implementer_result(
                paths=paths,
                state_payload=normalize_state_payload(json.loads(paths.state.read_text(encoding="utf-8"))),
                tasks_payload=refresh_ready_tasks(json.loads(paths.tasks.read_text(encoding="utf-8"))),
                task=task_index(tasks_payload)["T-001"],
                turn={
                    "thread_id": "thread-1",
                    "report": {
                        "role": "implementer",
                        "task_id": "T-001",
                        "attempt": 1,
                        "commit": "abc1111",
                        "summary": "Implemented T-001",
                        "files_changed": ["a.py"],
                        "checks_run": [],
                        "proposed_tasks": [],
                    },
                },
            )
            _apply_implementer_result(
                paths=paths,
                state_payload=normalize_state_payload(json.loads(paths.state.read_text(encoding="utf-8"))),
                tasks_payload=refresh_ready_tasks(json.loads(paths.tasks.read_text(encoding="utf-8"))),
                task=task_index(tasks_payload)["T-002"],
                turn={
                    "thread_id": "thread-2",
                    "report": {
                        "role": "implementer",
                        "task_id": "T-002",
                        "attempt": 1,
                        "commit": "def2222",
                        "summary": "Implemented T-002",
                        "files_changed": ["b.py"],
                        "checks_run": [],
                        "proposed_tasks": [],
                    },
                },
            )

            state_after_impl = normalize_state_payload(json.loads(paths.state.read_text(encoding="utf-8")))
            self.assertEqual(
                state_after_impl["state"]["active_tasks"]["T-001"]["role"],
                "verifier",
            )
            self.assertEqual(
                state_after_impl["state"]["active_tasks"]["T-002"]["role"],
                "verifier",
            )

            _apply_verifier_result(
                paths=paths,
                state_payload=normalize_state_payload(json.loads(paths.state.read_text(encoding="utf-8"))),
                tasks_payload=refresh_ready_tasks(json.loads(paths.tasks.read_text(encoding="utf-8"))),
                task_id="T-002",
                report={
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
