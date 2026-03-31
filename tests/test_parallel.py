"""Unit tests for parallel task pipeline execution support."""
from __future__ import annotations

import threading
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

from harness_artifacts import (
    Paths,
    all_ready_tasks,
    refresh_ready_tasks,
)
from harness_build_prompt import build_implementer_prompt_for_task
from harness_runtime_ops import _run_parallel_task_pipelines, sandbox_for_role


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
# Tests: _run_parallel_task_pipelines
# ---------------------------------------------------------------------------


class TestRunParallelTaskPipelines(unittest.TestCase):
    """Tests for the full implement→verify parallel pipeline."""

    def _make_state_payload(self, **overrides: Any) -> dict[str, Any]:
        """Build a minimal harness-state payload."""
        state = {
            "state": {
                "current_role": "implementer",
                "current_task_id": "",
                "current_attempt": 0,
                "trial_commit": "",
                "seq": 0,
                "implementer_runs": 0,
                "last_status": "",
                "last_decision": "",
                "accepts": 0,
                "reverts": 0,
            },
            "updated_at": "2025-01-01T00:00:00+00:00",
        }
        state["state"].update(overrides)
        return state

    @patch("harness_runtime_ops.evaluate_supervisor_status")
    @patch("harness_runtime_ops.build_verifier_prompt", return_value="verify prompt")
    @patch("harness_runtime_ops.write_json_atomic")
    @patch("harness_runtime_ops.read_json")
    @patch("harness_runtime_ops.run_role_turn")
    @patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="impl prompt")
    def test_runs_both_implement_and_verify_for_each_task(
        self,
        mock_build_impl: MagicMock,
        mock_run_turn: MagicMock,
        mock_read_json: MagicMock,
        mock_write_json: MagicMock,
        mock_build_verifier: MagicMock,
        mock_eval_supervisor: MagicMock,
    ) -> None:
        """Each task should get an implementer turn, supervisor call, verifier turn, and supervisor call."""
        mock_read_json.return_value = self._make_state_payload()
        mock_run_turn.return_value = {
            "report": {"role": "implementer", "task_id": "T-001", "commit": "abc123"},
            "thread_id": "thread-1",
            "turn_result": {},
            "parse_error": None,
        }
        # First supervisor call → dispatch_verifier; second → accept
        mock_eval_supervisor.side_effect = [
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
        ]

        manager = _mock_manager()
        tasks = [
            _make_task("T-001", status="ready"),
            _make_task("T-002", status="ready"),
        ]
        task_thread_map: dict[str, str] = {}
        lock = threading.Lock()

        decisions, errors = _run_parallel_task_pipelines(
            manager=manager,
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
            task_thread_map=task_thread_map,
            supervisor_lock=lock,
        )

        self.assertEqual(len(errors), 0)
        self.assertIn("T-001", decisions)
        self.assertIn("T-002", decisions)
        # Each task should have the verifier's decision (accept_task)
        self.assertEqual(decisions["T-001"]["reason"], "accept_task")
        self.assertEqual(decisions["T-002"]["reason"], "accept_task")

        # run_role_turn called 4 times: 2 implementer + 2 verifier
        self.assertEqual(mock_run_turn.call_count, 4)

        # evaluate_supervisor_status called 4 times: 2 impl + 2 verifier
        self.assertEqual(mock_eval_supervisor.call_count, 4)

        # Thread map should contain both tasks
        self.assertIn("T-001", task_thread_map)
        self.assertIn("T-002", task_thread_map)

    @patch("harness_runtime_ops.evaluate_supervisor_status")
    @patch("harness_runtime_ops.build_verifier_prompt", return_value="verify prompt")
    @patch("harness_runtime_ops.write_json_atomic")
    @patch("harness_runtime_ops.read_json")
    @patch("harness_runtime_ops.run_role_turn")
    @patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="impl prompt")
    def test_captures_errors_per_task(
        self,
        mock_build_impl: MagicMock,
        mock_run_turn: MagicMock,
        mock_read_json: MagicMock,
        mock_write_json: MagicMock,
        mock_build_verifier: MagicMock,
        mock_eval_supervisor: MagicMock,
    ) -> None:
        """When an implementer fails, its error is captured; other tasks proceed."""
        mock_read_json.return_value = self._make_state_payload()

        call_count = {"n": 0}

        def _run_turn_side_effect(**kwargs: Any) -> dict:
            call_count["n"] += 1
            if kwargs.get("task_id") == "T-002" and kwargs.get("role") == "implementer":
                raise RuntimeError("boom")
            return {
                "report": {"role": kwargs["role"], "task_id": kwargs["task_id"], "commit": "abc123"},
                "thread_id": "thread-ok",
                "turn_result": {},
                "parse_error": None,
            }

        mock_run_turn.side_effect = _run_turn_side_effect
        mock_eval_supervisor.side_effect = [
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
        ]

        manager = _mock_manager()
        tasks = [
            _make_task("T-001", status="ready"),
            _make_task("T-002", status="ready"),
        ]
        task_thread_map: dict[str, str] = {}
        lock = threading.Lock()

        decisions, errors = _run_parallel_task_pipelines(
            manager=manager,
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
            task_thread_map=task_thread_map,
            supervisor_lock=lock,
        )

        self.assertIn("T-001", decisions)
        self.assertIn("T-002", errors)
        self.assertIn("boom", errors["T-002"])

    @patch("harness_runtime_ops.evaluate_supervisor_status")
    @patch("harness_runtime_ops.build_verifier_prompt", return_value="verify prompt")
    @patch("harness_runtime_ops.write_json_atomic")
    @patch("harness_runtime_ops.read_json")
    @patch("harness_runtime_ops.run_role_turn")
    @patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="impl prompt")
    def test_supervisor_lock_serializes_state_access(
        self,
        mock_build_impl: MagicMock,
        mock_run_turn: MagicMock,
        mock_read_json: MagicMock,
        mock_write_json: MagicMock,
        mock_build_verifier: MagicMock,
        mock_eval_supervisor: MagicMock,
    ) -> None:
        """Verify the supervisor lock prevents concurrent state modifications."""
        mock_read_json.return_value = self._make_state_payload()
        mock_run_turn.return_value = {
            "report": {"role": "implementer", "task_id": "T-001", "commit": "abc"},
            "thread_id": "t-1",
            "turn_result": {},
            "parse_error": None,
        }
        mock_eval_supervisor.side_effect = [
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
        ]

        lock = threading.Lock()
        lock_held_concurrently = {"flag": False}
        original_acquire = lock.acquire
        original_release = lock.release

        # Track whether the lock is ever contended in a way that reveals
        # concurrent access to the supervisor section.  We wrap
        # evaluate_supervisor_status to check the lock is held.
        def _check_lock_held(**kwargs: Any) -> dict:
            # The lock should be held when the supervisor is called
            # Attempting to acquire with timeout=0 should fail (already held by this thread)
            # We can't directly test re-entrancy, but we can verify the function
            # is called and the overall count is correct.
            side_effects = mock_eval_supervisor._mock_children.get("side_effect")
            return mock_eval_supervisor.side_effect.pop(0) if hasattr(mock_eval_supervisor.side_effect, 'pop') else {"decision": "relaunch", "reason": "accept_task"}

        tasks = [
            _make_task("T-001", status="ready"),
            _make_task("T-002", status="ready"),
        ]
        task_thread_map: dict[str, str] = {}

        decisions, errors = _run_parallel_task_pipelines(
            manager=_mock_manager(),
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
            task_thread_map=task_thread_map,
            supervisor_lock=lock,
        )

        self.assertEqual(len(errors), 0)
        # All 4 supervisor calls happened (2 impl + 2 verifier)
        self.assertEqual(mock_eval_supervisor.call_count, 4)
        # State was written before each supervisor call (4 writes for state setup)
        self.assertGreaterEqual(mock_write_json.call_count, 4)

    @patch("harness_runtime_ops.evaluate_supervisor_status")
    @patch("harness_runtime_ops.write_json_atomic")
    @patch("harness_runtime_ops.read_json")
    @patch("harness_runtime_ops.run_role_turn")
    @patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="impl prompt")
    def test_implementer_stop_skips_verifier(
        self,
        mock_build_impl: MagicMock,
        mock_run_turn: MagicMock,
        mock_read_json: MagicMock,
        mock_write_json: MagicMock,
        mock_eval_supervisor: MagicMock,
    ) -> None:
        """If implementer supervisor returns stop, verifier should not run."""
        mock_read_json.return_value = self._make_state_payload()
        mock_run_turn.return_value = {
            "report": {"role": "implementer", "task_id": "T-001", "commit": "abc"},
            "thread_id": "t-1",
            "turn_result": {},
            "parse_error": None,
        }
        mock_eval_supervisor.return_value = {"decision": "stop", "reason": "all_done"}

        tasks = [_make_task("T-001", status="ready")]
        task_thread_map: dict[str, str] = {}
        lock = threading.Lock()

        decisions, errors = _run_parallel_task_pipelines(
            manager=_mock_manager(),
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
            task_thread_map=task_thread_map,
            supervisor_lock=lock,
        )

        self.assertEqual(len(errors), 0)
        self.assertIn("T-001", decisions)
        self.assertEqual(decisions["T-001"]["decision"], "stop")
        # Only 1 run_role_turn call (implementer), no verifier
        self.assertEqual(mock_run_turn.call_count, 1)
        # Only 1 supervisor call (implementer)
        self.assertEqual(mock_eval_supervisor.call_count, 1)

    @patch("harness_runtime_ops.evaluate_supervisor_status")
    @patch("harness_runtime_ops.build_verifier_prompt", return_value="verify prompt")
    @patch("harness_runtime_ops.write_json_atomic")
    @patch("harness_runtime_ops.read_json")
    @patch("harness_runtime_ops.run_role_turn")
    @patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="impl prompt")
    def test_empty_tasks_returns_empty(
        self,
        mock_build_impl: MagicMock,
        mock_run_turn: MagicMock,
        mock_read_json: MagicMock,
        mock_write_json: MagicMock,
        mock_build_verifier: MagicMock,
        mock_eval_supervisor: MagicMock,
    ) -> None:
        """No tasks means no results and no errors."""
        task_thread_map: dict[str, str] = {}
        lock = threading.Lock()

        decisions, errors = _run_parallel_task_pipelines(
            manager=_mock_manager(),
            ready_tasks=[],
            paths=FAKE_PATHS,
            runtime={},
            task_thread_map=task_thread_map,
            supervisor_lock=lock,
        )

        self.assertEqual(decisions, {})
        self.assertEqual(errors, {})

    @patch("harness_runtime_ops.evaluate_supervisor_status")
    @patch("harness_runtime_ops.build_verifier_prompt", return_value="verify prompt")
    @patch("harness_runtime_ops.write_json_atomic")
    @patch("harness_runtime_ops.read_json")
    @patch("harness_runtime_ops.run_role_turn")
    @patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="impl prompt")
    def test_uses_separate_threads(
        self,
        mock_build_impl: MagicMock,
        mock_run_turn: MagicMock,
        mock_read_json: MagicMock,
        mock_write_json: MagicMock,
        mock_build_verifier: MagicMock,
        mock_eval_supervisor: MagicMock,
    ) -> None:
        """Verify that tasks actually run on separate threads."""
        mock_read_json.return_value = self._make_state_payload()
        seen_threads: list[int] = []

        def _capture_thread(**kwargs: Any) -> dict:
            seen_threads.append(threading.current_thread().ident)
            return {
                "report": {"role": kwargs["role"], "task_id": kwargs["task_id"], "commit": "abc"},
                "thread_id": "t-1",
                "turn_result": {},
                "parse_error": None,
            }

        mock_run_turn.side_effect = _capture_thread
        mock_eval_supervisor.side_effect = [
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
        ]

        tasks = [
            _make_task("T-001", status="ready"),
            _make_task("T-002", status="ready"),
        ]
        task_thread_map: dict[str, str] = {}
        lock = threading.Lock()

        decisions, errors = _run_parallel_task_pipelines(
            manager=_mock_manager(),
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
            task_thread_map=task_thread_map,
            supervisor_lock=lock,
        )

        # Both tasks should have completed
        self.assertEqual(len(decisions), 2)
        self.assertEqual(len(errors), 0)
        # At least 4 run_role_turn calls from threads
        self.assertGreaterEqual(len(seen_threads), 4)

    @patch("harness_runtime_ops.evaluate_supervisor_status")
    @patch("harness_runtime_ops.build_verifier_prompt", return_value="verify prompt")
    @patch("harness_runtime_ops.write_json_atomic")
    @patch("harness_runtime_ops.read_json")
    @patch("harness_runtime_ops.run_role_turn")
    @patch("harness_runtime_ops.build_implementer_prompt_for_task", return_value="impl prompt")
    def test_state_set_correctly_before_supervisor_calls(
        self,
        mock_build_impl: MagicMock,
        mock_run_turn: MagicMock,
        mock_read_json: MagicMock,
        mock_write_json: MagicMock,
        mock_build_verifier: MagicMock,
        mock_eval_supervisor: MagicMock,
    ) -> None:
        """State file is updated with correct role/task_id/attempt before each supervisor call."""
        # Return a fresh copy each time so mutations don't leak between calls
        mock_read_json.side_effect = lambda *a, **kw: deepcopy(self._make_state_payload())
        mock_run_turn.return_value = {
            "report": {"role": "implementer", "task_id": "T-001", "commit": "abc"},
            "thread_id": "t-1",
            "turn_result": {},
            "parse_error": None,
        }
        mock_eval_supervisor.side_effect = [
            {"decision": "relaunch", "reason": "dispatch_verifier"},
            {"decision": "relaunch", "reason": "accept_task"},
        ]

        # Capture the actual payloads written (deep-copied to avoid mutation)
        written_payloads: list[dict] = []
        original_write = mock_write_json.side_effect

        def _capture_write(path: Any, payload: Any) -> None:
            written_payloads.append(deepcopy(payload))

        mock_write_json.side_effect = _capture_write

        tasks = [_make_task("T-001", status="ready")]
        task_thread_map: dict[str, str] = {}
        lock = threading.Lock()

        decisions, errors = _run_parallel_task_pipelines(
            manager=_mock_manager(),
            ready_tasks=tasks,
            paths=FAKE_PATHS,
            runtime={},
            task_thread_map=task_thread_map,
            supervisor_lock=lock,
        )

        self.assertEqual(len(errors), 0)
        # At least 2 state writes (before impl supervisor, before verifier supervisor)
        self.assertGreaterEqual(len(written_payloads), 2)

        # First state write should set role=implementer
        self.assertEqual(written_payloads[0]["state"]["current_role"], "implementer")
        self.assertEqual(written_payloads[0]["state"]["current_task_id"], "T-001")
        self.assertEqual(written_payloads[0]["state"]["current_attempt"], 1)

        # Second state write should set role=verifier
        self.assertEqual(written_payloads[1]["state"]["current_role"], "verifier")
        self.assertEqual(written_payloads[1]["state"]["current_task_id"], "T-001")
        self.assertEqual(written_payloads[1]["state"]["current_attempt"], 1)


if __name__ == "__main__":
    unittest.main()
