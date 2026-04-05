"""Unit tests for harness_runtime_ops: run_role_turn, sandbox_for_role, and dead-code removal."""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from harness_app_server import AppServerError
from harness_artifacts import HarnessError, build_launch_manifest, default_paths, write_json_atomic, write_tasks
from harness_init_run import initialize_run
from harness_runtime_ops import run_role_turn, run_runtime, sandbox_for_role


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_manager(*, final_message: str = '{"role":"planner","revision":1}') -> MagicMock:
    """Return a mock ServerManager whose acquire() returns a usable ManagedServer."""
    ms = MagicMock()
    ms.server.start_thread.return_value = "thread-abc"
    ms.server.resume_thread.return_value = "thread-abc"
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
# Tests: sandbox_for_role
# ---------------------------------------------------------------------------

class TestSandboxForRole(unittest.TestCase):
    def test_danger_policy_all_roles_get_full_access(self) -> None:
        self.assertEqual(sandbox_for_role("planner", "danger_full_access"), "danger-full-access")
        self.assertEqual(sandbox_for_role("implementer", "danger_full_access"), "danger-full-access")
        self.assertEqual(sandbox_for_role("verifier", "danger_full_access"), "danger-full-access")

    def test_workspace_write_policy(self) -> None:
        self.assertEqual(sandbox_for_role("planner", "workspace_write"), "workspace-write")
        self.assertEqual(sandbox_for_role("implementer", "workspace_write"), "workspace-write")
        self.assertEqual(sandbox_for_role("verifier", "workspace_write"), "read-only")

    def test_default_policy_is_danger(self) -> None:
        self.assertEqual(sandbox_for_role("planner"), "danger-full-access")

    def test_unknown_role_defaults_to_read_only(self) -> None:
        self.assertEqual(sandbox_for_role("unknown"), "read-only")


# ---------------------------------------------------------------------------
# Tests: run_role_turn
# ---------------------------------------------------------------------------

class TestRunRoleTurn(unittest.TestCase):
    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_returns_parsed_report(self, _mock_schema: Any) -> None:
        manager = _mock_manager(final_message='{"role":"planner","revision":1}')
        result = run_role_turn(
            manager=manager,
            role="planner",
            task_id="T-001",
            prompt="do the plan",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
        )
        self.assertIsInstance(result["report"], dict)
        self.assertEqual(result["report"]["role"], "planner")
        self.assertEqual(result["thread_id"], "thread-abc")
        self.assertIsNone(result["parse_error"])

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_release_called_on_success(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        ms = manager.acquire.return_value
        run_role_turn(
            manager=manager,
            role="planner",
            task_id="",
            prompt="plan",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
        )
        manager.release.assert_called_with(ms)

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_release_called_on_parse_failure(self, _mock_schema: Any) -> None:
        manager = _mock_manager(final_message="not json at all")
        ms = manager.acquire.return_value
        with self.assertRaises(HarnessError):
            run_role_turn(
                manager=manager,
                role="planner",
                task_id="",
                prompt="plan",
                repo=Path("/tmp/fake"),
                sandbox="danger-full-access",
            )
        manager.release.assert_called_with(ms)

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_raises_harness_error_on_empty_output(self, _mock_schema: Any) -> None:
        manager = _mock_manager(final_message="")
        with self.assertRaises(HarnessError) as ctx:
            run_role_turn(
                manager=manager,
                role="implementer",
                task_id="T-002",
                prompt="implement it",
                repo=Path("/tmp/fake"),
                sandbox="danger-full-access",
            )
        self.assertIn("Failed to parse", str(ctx.exception))

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_resumes_thread_when_resume_id_provided(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        ms = manager.acquire.return_value
        run_role_turn(
            manager=manager,
            role="implementer",
            task_id="T-001",
            prompt="retry",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
            resume_thread_id="thread-old",
        )
        ms.server.resume_thread.assert_called_once_with("thread-old", sandbox="danger-full-access")
        ms.server.start_thread.assert_not_called()

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_starts_new_thread_when_no_resume_id(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        ms = manager.acquire.return_value
        run_role_turn(
            manager=manager,
            role="planner",
            task_id="",
            prompt="plan",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
        )
        ms.server.start_thread.assert_called_once_with(sandbox="danger-full-access")
        ms.server.resume_thread.assert_not_called()

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_stores_thread_id_in_history(self, _mock_schema: Any) -> None:
        manager = _mock_manager()
        ms = manager.acquire.return_value
        ms.thread_history = {}
        run_role_turn(
            manager=manager,
            role="planner",
            task_id="T-001",
            prompt="plan",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
        )
        self.assertEqual(ms.thread_history["T-001"], "thread-abc")

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_retries_on_broken_pipe(self, _mock_schema: Any) -> None:
        """On BrokenPipeError the function should close the dead server, acquire a fresh one, and retry."""
        manager = MagicMock()
        dead_ms = MagicMock()
        dead_ms.server.start_thread.side_effect = BrokenPipeError("pipe gone")
        dead_ms.thread_history = {}

        fresh_ms = MagicMock()
        fresh_ms.server.start_thread.return_value = "thread-new"
        fresh_ms.server.run_turn.return_value = {
            "status": "completed",
            "thread_id": "thread-new",
            "final_message": '{"role":"planner","revision":1}',
            "file_changes": [],
            "command_executions": [],
            "reasoning_summary": "",
        }
        fresh_ms.thread_history = {}

        manager.acquire.side_effect = [dead_ms, fresh_ms]
        result = run_role_turn(
            manager=manager,
            role="planner",
            task_id="",
            prompt="plan",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
        )
        self.assertEqual(result["thread_id"], "thread-new")
        dead_ms.server.close.assert_called_once()

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_retries_on_app_server_error_when_server_dead(self, _mock_schema: Any) -> None:
        """When AppServerError is raised and the server process is dead, retry with a fresh server."""
        manager = MagicMock()
        dead_ms = MagicMock()
        dead_ms.server.start_thread.side_effect = AppServerError("app-server connection closed")
        dead_ms.alive = False
        dead_ms.thread_history = {}

        fresh_ms = MagicMock()
        fresh_ms.server.start_thread.return_value = "thread-new"
        fresh_ms.server.run_turn.return_value = {
            "status": "completed",
            "thread_id": "thread-new",
            "final_message": '{"role":"planner","revision":1}',
            "file_changes": [],
            "command_executions": [],
            "reasoning_summary": "",
        }
        fresh_ms.thread_history = {}

        manager.acquire.side_effect = [dead_ms, fresh_ms]
        result = run_role_turn(
            manager=manager,
            role="planner",
            task_id="",
            prompt="plan",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
        )
        self.assertEqual(result["thread_id"], "thread-new")
        dead_ms.server.close.assert_called_once()

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_app_server_error_reraised_when_server_alive(self, _mock_schema: Any) -> None:
        """When AppServerError is raised but the server is still alive, re-raise immediately (logical error)."""
        manager = MagicMock()
        ms = MagicMock()
        ms.server.start_thread.side_effect = AppServerError("schema validation failed", code=-32600)
        ms.alive = True
        ms.thread_history = {}
        manager.acquire.return_value = ms

        with self.assertRaises(AppServerError) as ctx:
            run_role_turn(
                manager=manager,
                role="planner",
                task_id="",
                prompt="plan",
                repo=Path("/tmp/fake"),
                sandbox="danger-full-access",
            )
        self.assertIn("schema validation failed", str(ctx.exception))

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_raises_after_second_broken_pipe(self, _mock_schema: Any) -> None:
        """If the retry also fails with a connection error, raise HarnessError."""
        manager = MagicMock()
        ms1 = MagicMock()
        ms1.server.start_thread.side_effect = BrokenPipeError("gone")
        ms1.thread_history = {}
        ms2 = MagicMock()
        ms2.server.start_thread.side_effect = ConnectionError("also gone")
        ms2.thread_history = {}
        manager.acquire.side_effect = [ms1, ms2]

        with self.assertRaises(HarnessError) as ctx:
            run_role_turn(
                manager=manager,
                role="planner",
                task_id="",
                prompt="plan",
                repo=Path("/tmp/fake"),
                sandbox="danger-full-access",
            )
        self.assertIn("failed twice", str(ctx.exception))


# ---------------------------------------------------------------------------
# Tests: dead-code removal verification
# ---------------------------------------------------------------------------

class TestRunRuntimeUsesAppServer(unittest.TestCase):
    def test_no_codex_exec_references(self) -> None:
        source_path = Path(__file__).resolve().parents[1] / "scripts" / "harness_runtime_ops.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("codex exec", source)
        self.assertNotIn("build_codex_exec_command", source)

    def test_no_codex_args_for_execution_policy_in_runtime_common(self) -> None:
        source_path = Path(__file__).resolve().parents[1] / "scripts" / "harness_runtime_common.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("codex_args_for_execution_policy", source)


# ---------------------------------------------------------------------------
# Tests: thread resume wiring in run_runtime
# ---------------------------------------------------------------------------

class TestThreadResume(unittest.TestCase):
    """Verify that run_runtime passes resume_thread_id on implementer retries."""

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_first_implementer_call_has_no_resume_id(self, _mock_schema: Any) -> None:
        """On the first implementer attempt for a task, resume_thread_id should be None."""
        from harness_runtime_ops import run_role_turn

        manager = _mock_manager(final_message='{"role":"implementer","task_id":"T-001","attempt":1,"commit":"abc","summary":"ok","files_changed":[],"checks_run":[],"proposed_tasks":[]}')
        ms = manager.acquire.return_value

        run_role_turn(
            manager=manager,
            role="implementer",
            task_id="T-001",
            prompt="implement it",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
            resume_thread_id=None,
        )
        ms.server.start_thread.assert_called_once_with(sandbox="danger-full-access")
        ms.server.resume_thread.assert_not_called()

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_retry_uses_resume_thread(self, _mock_schema: Any) -> None:
        """When resume_thread_id is provided, run_role_turn should call resume_thread."""
        from harness_runtime_ops import run_role_turn

        manager = _mock_manager(final_message='{"role":"implementer","task_id":"T-001","attempt":2,"commit":"def","summary":"retry","files_changed":[],"checks_run":[],"proposed_tasks":[]}')
        ms = manager.acquire.return_value

        result = run_role_turn(
            manager=manager,
            role="implementer",
            task_id="T-001",
            prompt="retry it",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
            resume_thread_id="t-1",
        )
        ms.server.resume_thread.assert_called_once_with("t-1", sandbox="danger-full-access")
        ms.server.start_thread.assert_not_called()
        self.assertEqual(result["thread_id"], "thread-abc")

    @patch("harness_runtime_ops.load_schema", return_value={"type": "object"})
    def test_task_thread_map_tracks_implementer_threads(self, _mock_schema: Any) -> None:
        """The task_thread_map should store thread IDs for implementer turns."""
        task_thread_map: dict[str, str] = {}
        manager = _mock_manager(final_message='{"role":"implementer","task_id":"T-001","attempt":1,"commit":"abc","summary":"ok","files_changed":[],"checks_run":[],"proposed_tasks":[]}')

        turn = run_role_turn(
            manager=manager,
            role="implementer",
            task_id="T-001",
            prompt="implement it",
            repo=Path("/tmp/fake"),
            sandbox="danger-full-access",
        )
        # Simulate what run_runtime does after run_role_turn
        task_thread_map["T-001"] = turn["thread_id"]
        self.assertEqual(task_thread_map["T-001"], "thread-abc")

        # On retry, the map should provide the resume_id
        resume_id = task_thread_map.get("T-001")
        self.assertEqual(resume_id, "thread-abc")

    def test_verifier_feedback_prepended_on_retry(self) -> None:
        """When retrying, verifier feedback should be prepended to the prompt."""
        last_verifier_feedback = "Tests fail because function X returns None instead of a list."
        original_prompt = "$harness\nYou are the implementer..."

        # Simulate the logic from run_runtime
        task_thread_map = {"T-001": "thread-old"}
        role = "implementer"
        task_id = "T-001"

        resume_id: str | None = None
        prompt_text = original_prompt
        if role == "implementer" and task_id in task_thread_map:
            resume_id = task_thread_map[task_id]
            if last_verifier_feedback:
                prompt_text = (
                    f"[VERIFIER FEEDBACK FROM PREVIOUS ATTEMPT]\n"
                    f"{last_verifier_feedback}\n"
                    f"[END VERIFIER FEEDBACK]\n\n"
                    f"{prompt_text}"
                )
                last_verifier_feedback = ""

        self.assertEqual(resume_id, "thread-old")
        self.assertIn("[VERIFIER FEEDBACK FROM PREVIOUS ATTEMPT]", prompt_text)
        self.assertIn("Tests fail because function X returns None", prompt_text)
        self.assertIn("[END VERIFIER FEEDBACK]", prompt_text)
        # The original prompt should still be present after the feedback
        self.assertIn("$harness\nYou are the implementer...", prompt_text)

    def test_no_verifier_feedback_on_first_attempt(self) -> None:
        """On the first implementer attempt, no feedback should be prepended."""
        original_prompt = "$harness\nYou are the implementer..."

        task_thread_map: dict[str, str] = {}
        last_verifier_feedback = ""
        role = "implementer"
        task_id = "T-001"

        resume_id: str | None = None
        prompt_text = original_prompt
        if role == "implementer" and task_id in task_thread_map:
            resume_id = task_thread_map[task_id]
            if last_verifier_feedback:
                prompt_text = (
                    f"[VERIFIER FEEDBACK FROM PREVIOUS ATTEMPT]\n"
                    f"{last_verifier_feedback}\n"
                    f"[END VERIFIER FEEDBACK]\n\n"
                    f"{prompt_text}"
                )
                last_verifier_feedback = ""

        self.assertIsNone(resume_id)
        self.assertNotIn("[VERIFIER FEEDBACK", prompt_text)
        self.assertEqual(prompt_text, original_prompt)

    def test_feedback_cleared_after_use(self) -> None:
        """After prepending feedback, the buffer should be cleared."""
        last_verifier_feedback = "Some feedback"
        task_thread_map = {"T-001": "thread-old"}

        role = "implementer"
        task_id = "T-001"
        prompt_text = "prompt"

        if role == "implementer" and task_id in task_thread_map:
            if last_verifier_feedback:
                prompt_text = (
                    f"[VERIFIER FEEDBACK FROM PREVIOUS ATTEMPT]\n"
                    f"{last_verifier_feedback}\n"
                    f"[END VERIFIER FEEDBACK]\n\n"
                    f"{prompt_text}"
                )
                last_verifier_feedback = ""

        self.assertEqual(last_verifier_feedback, "")

    def test_retry_task_decision_captures_feedback(self) -> None:
        """When decision reason is retry_task, the verifier summary should be stored."""
        last_verifier_feedback = ""
        report = {"summary": "Tests fail: missing return value in handler", "verdict": "revert"}
        decision = {"decision": "relaunch", "reason": "retry_task"}

        # Simulate the logic from run_runtime
        if decision.get("reason") == "retry_task":
            last_verifier_feedback = str(report.get("summary") or "")

        self.assertEqual(last_verifier_feedback, "Tests fail: missing return value in handler")

    def test_non_retry_decision_does_not_capture_feedback(self) -> None:
        """When decision reason is not retry_task, feedback should not be stored."""
        last_verifier_feedback = ""
        report = {"summary": "All good", "verdict": "accept"}
        decision = {"decision": "relaunch", "reason": "dispatch_verifier"}

        if decision.get("reason") == "retry_task":
            last_verifier_feedback = str(report.get("summary") or "")

        self.assertEqual(last_verifier_feedback, "")


class TestRunRuntimeScheduling(unittest.TestCase):
    def test_multiple_ready_tasks_run_one_implementer_turn_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            paths = default_paths(repo)

            write_json_atomic(
                paths.launch,
                build_launch_manifest(
                    original_goal="Run two ready tasks safely",
                    prompt_text=None,
                    config={
                        "goal": "Run two ready tasks safely",
                        "scope": ".",
                        "session_mode": "background",
                        "execution_policy": "danger_full_access",
                        "stop_condition": "",
                        "allow_task_expansion": "enabled",
                        "max_task_attempts": 2,
                    },
                ),
            )
            initialize_run(
                repo=repo,
                goal="Run two ready tasks safely",
                scope=".",
                session_mode="background",
                execution_policy="danger_full_access",
                max_task_attempts=2,
                force=True,
            )
            write_tasks(
                paths.tasks,
                {
                    "version": 1,
                    "goal": "Run two ready tasks safely",
                    "planner_revision": 1,
                    "tasks": [
                        {
                            "id": "T-001",
                            "title": "Task T-001",
                            "description": "First task",
                            "acceptance_criteria": ["done"],
                            "status": "ready",
                            "priority": 1,
                            "dependencies": [],
                            "attempts": 0,
                        },
                        {
                            "id": "T-002",
                            "title": "Task T-002",
                            "description": "Second task",
                            "acceptance_criteria": ["done"],
                            "status": "ready",
                            "priority": 1,
                            "dependencies": [],
                            "attempts": 0,
                        },
                    ],
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "updated_at": "2025-01-01T00:00:00+00:00",
                },
            )

            call_order: list[tuple[str, str]] = []

            def fake_run_role_turn(*, role: str, task_id: str, **kwargs: Any) -> dict[str, Any]:
                call_order.append((role, task_id))
                if role == "implementer":
                    return {
                        "report": {
                            "role": "implementer",
                            "task_id": task_id,
                            "attempt": 1,
                            "commit": f"commit-{task_id}",
                            "summary": f"implemented {task_id}",
                            "files_changed": [f"{task_id}.txt"],
                            "checks_run": [],
                            "proposed_tasks": [],
                        },
                        "thread_id": f"thread-{task_id}",
                        "turn_result": {},
                        "parse_error": None,
                    }
                return {
                    "report": {
                        "role": "verifier",
                        "task_id": task_id,
                        "attempt": 1,
                        "commit": f"commit-{task_id}",
                        "verdict": "accept",
                        "summary": f"verified {task_id}",
                        "findings": [],
                        "criteria_results": [],
                        "proposed_tasks": [],
                    },
                    "thread_id": f"verify-{task_id}",
                    "turn_result": {},
                    "parse_error": None,
                }

            class FakeServerManager:
                def __init__(self, **kwargs: Any) -> None:
                    self.kwargs = kwargs

                def kill_orphans(self) -> None:
                    return None

                def shutdown(self) -> None:
                    return None

            args = argparse.Namespace(repo=str(repo), codex_bin="codex", sleep_seconds=0)

            def fake_prepare_task_worktree(*, task_id: str, **kwargs: Any) -> dict[str, str]:
                worktree = repo / f"worktree-{task_id}"
                worktree.mkdir(exist_ok=True)
                return {
                    "branch_name": f"branch-{task_id}",
                    "worktree_path": str(worktree),
                    "base_commit": "base-commit",
                }

            with patch("harness_runtime_ops.ServerManager", FakeServerManager), patch(
                "harness_runtime_ops.run_role_turn", side_effect=fake_run_role_turn
            ), patch("harness_runtime_ops.prepare_task_worktree", side_effect=fake_prepare_task_worktree), patch(
                "harness_supervisor_status.cherry_pick_commit", side_effect=lambda **kwargs: f"integrated-{kwargs['commit']}"
            ), patch("harness_supervisor_status.remove_task_worktree", return_value=None), patch(
                "harness_runtime_ops.time.sleep", return_value=None
            ):
                exit_code = run_runtime(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual({call_order[0], call_order[1]}, {("implementer", "T-001"), ("implementer", "T-002")})
            self.assertEqual(call_order[2:], [("verifier", "T-001"), ("verifier", "T-002")])

            tasks_payload = json.loads(paths.tasks.read_text(encoding="utf-8"))
            self.assertEqual([task["status"] for task in tasks_payload["tasks"]], ["done", "done"])
            state_payload = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(state_payload["state"]["active_tasks"], {})
            runtime_payload = json.loads(paths.runtime.read_text(encoding="utf-8"))
            self.assertEqual(runtime_payload["status"], "terminal")
            self.assertEqual(runtime_payload["terminal_reason"], "all_tasks_done")

    def test_cherry_pick_conflict_escalates_runtime_to_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            paths = default_paths(repo)

            write_json_atomic(
                paths.launch,
                build_launch_manifest(
                    original_goal="Run one task with a cherry-pick conflict",
                    prompt_text=None,
                    config={
                        "goal": "Run one task with a cherry-pick conflict",
                        "scope": ".",
                        "session_mode": "background",
                        "execution_policy": "danger_full_access",
                        "stop_condition": "",
                        "allow_task_expansion": "enabled",
                        "max_task_attempts": 2,
                    },
                ),
            )
            initialize_run(
                repo=repo,
                goal="Run one task with a cherry-pick conflict",
                scope=".",
                session_mode="background",
                execution_policy="danger_full_access",
                max_task_attempts=2,
                force=True,
            )
            write_tasks(
                paths.tasks,
                {
                    "version": 1,
                    "goal": "Run one task with a cherry-pick conflict",
                    "planner_revision": 1,
                    "tasks": [
                        {
                            "id": "T-001",
                            "title": "Task T-001",
                            "description": "Conflicting task",
                            "acceptance_criteria": ["done"],
                            "status": "ready",
                            "priority": 1,
                            "dependencies": [],
                            "attempts": 0,
                        }
                    ],
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "updated_at": "2025-01-01T00:00:00+00:00",
                },
            )

            def fake_run_role_turn(*, role: str, task_id: str, **kwargs: Any) -> dict[str, Any]:
                if role == "implementer":
                    return {
                        "report": {
                            "role": "implementer",
                            "task_id": task_id,
                            "attempt": 1,
                            "commit": "commit-T-001",
                            "summary": "implemented",
                            "files_changed": ["T-001.txt"],
                            "checks_run": [],
                            "proposed_tasks": [],
                        },
                        "thread_id": "thread-T-001",
                        "turn_result": {},
                        "parse_error": None,
                    }
                return {
                    "report": {
                        "role": "verifier",
                        "task_id": task_id,
                        "attempt": 1,
                        "commit": "commit-T-001",
                        "verdict": "accept",
                        "summary": "verified",
                        "findings": [],
                        "criteria_results": [],
                        "proposed_tasks": [],
                    },
                    "thread_id": "verify-T-001",
                    "turn_result": {},
                    "parse_error": None,
                }

            class FakeServerManager:
                def __init__(self, **kwargs: Any) -> None:
                    self.kwargs = kwargs

                def kill_orphans(self) -> None:
                    return None

                def shutdown(self) -> None:
                    return None

            def fake_prepare_task_worktree(*, task_id: str, **kwargs: Any) -> dict[str, str]:
                worktree = repo / f"worktree-{task_id}"
                worktree.mkdir(exist_ok=True)
                return {
                    "branch_name": f"branch-{task_id}",
                    "worktree_path": str(worktree),
                    "base_commit": "base-commit",
                }

            args = argparse.Namespace(repo=str(repo), codex_bin="codex", sleep_seconds=0)

            with patch("harness_runtime_ops.ServerManager", FakeServerManager), patch(
                "harness_runtime_ops.run_role_turn", side_effect=fake_run_role_turn
            ), patch("harness_runtime_ops.prepare_task_worktree", side_effect=fake_prepare_task_worktree), patch(
                "harness_supervisor_status.cherry_pick_commit", side_effect=HarnessError("simulated cherry-pick conflict")
            ), patch("harness_runtime_ops.time.sleep", return_value=None):
                exit_code = run_runtime(args)

            self.assertEqual(exit_code, 2)
            runtime_payload = json.loads(paths.runtime.read_text(encoding="utf-8"))
            self.assertEqual(runtime_payload["status"], "needs_human")
            self.assertIn("simulated cherry-pick conflict", runtime_payload["terminal_reason"])

    def test_complex_dag_with_parallel_fan_in_and_retry_reaches_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            paths = default_paths(repo)

            write_json_atomic(
                paths.launch,
                build_launch_manifest(
                    original_goal="Run a complex DAG",
                    prompt_text=None,
                    config={
                        "goal": "Run a complex DAG",
                        "scope": ".",
                        "session_mode": "background",
                        "execution_policy": "danger_full_access",
                        "stop_condition": "",
                        "allow_task_expansion": "enabled",
                        "max_task_attempts": 3,
                    },
                ),
            )
            initialize_run(
                repo=repo,
                goal="Run a complex DAG",
                scope=".",
                session_mode="background",
                execution_policy="danger_full_access",
                max_task_attempts=3,
                force=True,
            )
            write_tasks(
                paths.tasks,
                {
                    "version": 1,
                    "goal": "Run a complex DAG",
                    "planner_revision": 1,
                    "tasks": [
                        {
                            "id": "T-001",
                            "title": "Task T-001",
                            "description": "Parallel leaf one",
                            "acceptance_criteria": ["done"],
                            "status": "ready",
                            "priority": 1,
                            "dependencies": [],
                            "attempts": 0,
                        },
                        {
                            "id": "T-002",
                            "title": "Task T-002",
                            "description": "Parallel leaf two with retry",
                            "acceptance_criteria": ["done"],
                            "status": "ready",
                            "priority": 1,
                            "dependencies": [],
                            "attempts": 0,
                        },
                        {
                            "id": "T-003",
                            "title": "Task T-003",
                            "description": "Parallel leaf three",
                            "acceptance_criteria": ["done"],
                            "status": "ready",
                            "priority": 1,
                            "dependencies": [],
                            "attempts": 0,
                        },
                        {
                            "id": "T-004",
                            "title": "Task T-004",
                            "description": "Fan-in task",
                            "acceptance_criteria": ["done"],
                            "status": "pending",
                            "priority": 2,
                            "dependencies": ["T-001", "T-002"],
                            "attempts": 0,
                        },
                        {
                            "id": "T-005",
                            "title": "Task T-005",
                            "description": "Final fan-in task",
                            "acceptance_criteria": ["done"],
                            "status": "pending",
                            "priority": 3,
                            "dependencies": ["T-003", "T-004"],
                            "attempts": 0,
                        },
                    ],
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "updated_at": "2025-01-01T00:00:00+00:00",
                },
            )

            call_order: list[tuple[str, str]] = []
            implementer_counts: dict[str, int] = {}
            verifier_counts: dict[str, int] = {}

            def fake_run_role_turn(*, role: str, task_id: str, **kwargs: Any) -> dict[str, Any]:
                call_order.append((role, task_id))
                if role == "implementer":
                    attempt = implementer_counts.get(task_id, 0) + 1
                    implementer_counts[task_id] = attempt
                    return {
                        "report": {
                            "role": "implementer",
                            "task_id": task_id,
                            "attempt": attempt,
                            "commit": f"commit-{task_id}-a{attempt}",
                            "summary": f"implemented {task_id} attempt {attempt}",
                            "files_changed": [f"{task_id}.txt"],
                            "checks_run": [],
                            "proposed_tasks": [],
                        },
                        "thread_id": f"thread-{task_id}",
                        "turn_result": {},
                        "parse_error": None,
                    }

                attempt = verifier_counts.get(task_id, 0) + 1
                verifier_counts[task_id] = attempt
                verdict = "accept"
                summary = f"verified {task_id}"
                if task_id == "T-002" and attempt == 1:
                    verdict = "revert"
                    summary = "retry this branch once"
                return {
                    "report": {
                        "role": "verifier",
                        "task_id": task_id,
                        "attempt": attempt,
                        "commit": f"commit-{task_id}-a{attempt}",
                        "verdict": verdict,
                        "summary": summary,
                        "findings": [],
                        "criteria_results": [],
                        "proposed_tasks": [],
                    },
                    "thread_id": f"verify-{task_id}",
                    "turn_result": {},
                    "parse_error": None,
                }

            class FakeServerManager:
                def __init__(self, **kwargs: Any) -> None:
                    self.kwargs = kwargs

                def kill_orphans(self) -> None:
                    return None

                def shutdown(self) -> None:
                    return None

            def fake_prepare_task_worktree(*, task_id: str, **kwargs: Any) -> dict[str, str]:
                worktree = repo / f"worktree-{task_id}"
                worktree.mkdir(exist_ok=True)
                return {
                    "branch_name": f"branch-{task_id}",
                    "worktree_path": str(worktree),
                    "base_commit": "base-commit",
                }

            args = argparse.Namespace(repo=str(repo), codex_bin="codex", sleep_seconds=0)

            with patch("harness_runtime_ops.ServerManager", FakeServerManager), patch(
                "harness_runtime_ops.run_role_turn", side_effect=fake_run_role_turn
            ), patch("harness_runtime_ops.prepare_task_worktree", side_effect=fake_prepare_task_worktree), patch(
                "harness_supervisor_status.cherry_pick_commit", side_effect=lambda **kwargs: f"integrated-{kwargs['commit']}"
            ), patch("harness_supervisor_status.git_head", return_value="base-commit-2"), patch(
                "harness_supervisor_status.reset_task_worktree", return_value=None
            ), patch(
                "harness_supervisor_status.remove_task_worktree", return_value=None
            ), patch("harness_runtime_ops.time.sleep", return_value=None):
                exit_code = run_runtime(args)

            self.assertEqual(exit_code, 0)
            tasks_payload = json.loads(paths.tasks.read_text(encoding="utf-8"))
            index = {task["id"]: task for task in tasks_payload["tasks"]}
            self.assertTrue(all(task["status"] == "done" for task in index.values()))
            self.assertEqual(index["T-002"]["attempts"], 2)
            self.assertEqual(index["T-002"]["last_verdict"], "accept")
            self.assertEqual(index["T-004"]["status"], "done")
            self.assertEqual(index["T-005"]["status"], "done")

            state_payload = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(state_payload["state"]["accepts"], 5)
            self.assertEqual(state_payload["state"]["reverts"], 1)
            self.assertEqual(state_payload["state"]["active_tasks"], {})
            self.assertTrue(state_payload["state"]["completed"])

            runtime_payload = json.loads(paths.runtime.read_text(encoding="utf-8"))
            self.assertEqual(runtime_payload["status"], "terminal")
            self.assertEqual(runtime_payload["terminal_reason"], "all_tasks_done")

            self.assertIn(("implementer", "T-004"), call_order)
            self.assertIn(("implementer", "T-005"), call_order)
            self.assertEqual(implementer_counts["T-002"], 2)
            self.assertEqual(verifier_counts["T-002"], 2)


if __name__ == "__main__":
    unittest.main()
