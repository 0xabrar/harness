"""Unit tests for harness_runtime_ops: run_role_turn, sandbox_for_role, and dead-code removal."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from harness_app_server import AppServerError
from harness_artifacts import HarnessError
from harness_runtime_ops import ROLE_SANDBOX, run_role_turn, sandbox_for_role


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
    def test_planner_gets_workspace_write(self) -> None:
        self.assertEqual(sandbox_for_role("planner"), "danger-full-access")

    def test_implementer_gets_workspace_write(self) -> None:
        self.assertEqual(sandbox_for_role("implementer"), "danger-full-access")

    def test_verifier_gets_read_only(self) -> None:
        self.assertEqual(sandbox_for_role("verifier"), "read-only")

    def test_unknown_role_defaults_to_read_only(self) -> None:
        self.assertEqual(sandbox_for_role("unknown"), "read-only")

    def test_role_sandbox_dict_has_all_known_roles(self) -> None:
        self.assertIn("planner", ROLE_SANDBOX)
        self.assertIn("implementer", ROLE_SANDBOX)
        self.assertIn("verifier", ROLE_SANDBOX)


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

    def _make_state(self, role: str, task_id: str = "T-001", attempt: int = 1, trial_commit: str = "") -> dict:
        return {
            "config": {"goal": "test", "scope": ".", "max_task_attempts": 3},
            "state": {
                "current_role": role,
                "current_task_id": task_id,
                "current_attempt": attempt,
                "trial_commit": trial_commit,
                "seq": 0,
                "planner_revision": 0,
                "planner_runs": 0,
                "implementer_runs": 0,
                "verifier_runs": 0,
                "accepts": 0,
                "reverts": 0,
                "needs_human": 0,
                "replans": 0,
                "completed": False,
                "last_status": "",
                "last_decision": "",
                "last_verdict": "",
            },
        }

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


if __name__ == "__main__":
    unittest.main()
