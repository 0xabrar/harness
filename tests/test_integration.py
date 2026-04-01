"""Integration tests: full supervisor cycle using report_override and ServerManager lifecycle."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_app_server import CodexAppServer, ManagedServer, ServerManager, SERVERS_STATE_FILENAME  # noqa: E402
from harness_artifacts import default_paths, load_tasks, read_json, write_tasks  # noqa: E402
from harness_init_run import initialize_run  # noqa: E402
from harness_supervisor_status import evaluate_supervisor_status  # noqa: E402


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def setup_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "initial commit")
    return repo


def seed_task(repo: Path, task_id: str = "1", title: str = "Test task") -> None:
    paths = default_paths(repo)
    tasks = load_tasks(paths.tasks)
    tasks["tasks"].append({
        "id": task_id,
        "title": title,
        "description": "Do something",
        "acceptance_criteria": ["It works"],
        "status": "ready",
        "priority": 1,
        "dependencies": [],
        "attempts": 0,
    })
    tasks["planner_revision"] = 1
    write_tasks(paths.tasks, tasks)


class TestFullAcceptCycle(unittest.TestCase):
    """Planner -> implementer -> verifier (accept) -> stop, using report_override."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.repo = setup_repo(Path(self._tmpdir))
        initialize_run(
            repo=self.repo,
            goal="Integration test goal",
            scope=".",
            session_mode="background",
            execution_policy="danger_full_access",
            stop_condition="",
            allow_task_expansion="enabled",
            max_task_attempts=3,
            force=True,
        )
        self.paths = default_paths(self.repo)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_full_planner_implementer_verifier_accept_cycle(self) -> None:
        # Seed a task into tasks.json (simulating what the planner would do)
        seed_task(self.repo, task_id="1", title="Implement feature")

        # --- Step 1: Planner turn (report_override) ---
        planner_report = {
            "role": "planner",
            "revision": 1,
            "summary": "Created task 1",
            "task_changes": {"added": ["1"], "updated": [], "closed": []},
            "planner_requested_reason": "initial_plan",
        }
        decision1 = evaluate_supervisor_status(repo=self.repo, report_override=planner_report)
        self.assertEqual(decision1["decision"], "relaunch")
        self.assertEqual(decision1["reason"], "dispatch_implementer")

        # State should now point to implementer for task 1
        state = read_json(self.paths.state)
        self.assertEqual(state["state"]["active_tasks"]["1"]["role"], "implementer")
        self.assertEqual(state["state"]["active_tasks"]["1"]["attempt"], 1)

        # --- Step 2: Implementer turn ---
        # Create a real git commit so the verifier flow has a valid commit
        (self.repo / "feature.py").write_text("print('hello')\n", encoding="utf-8")
        git(self.repo, "add", "feature.py")
        git(self.repo, "commit", "-m", "implement feature")
        commit = git(self.repo, "rev-parse", "--short", "HEAD")

        impl_report = {
            "role": "implementer",
            "task_id": "1",
            "attempt": 1,
            "commit": commit,
            "summary": "Implemented the feature",
            "files_changed": ["feature.py"],
            "checks_run": [],
            "proposed_tasks": [],
        }
        decision2 = evaluate_supervisor_status(repo=self.repo, report_override=impl_report)
        self.assertEqual(decision2["decision"], "relaunch")
        self.assertEqual(decision2["reason"], "dispatch_verifier")

        # State should now point to verifier
        state = read_json(self.paths.state)
        self.assertEqual(state["state"]["active_tasks"]["1"]["role"], "verifier")
        self.assertEqual(state["state"]["active_tasks"]["1"]["trial_commit"], commit)

        # --- Step 3: Verifier accepts ---
        verifier_report = {
            "role": "verifier",
            "task_id": "1",
            "attempt": 1,
            "commit": commit,
            "verdict": "accept",
            "summary": "All acceptance criteria passed",
            "findings": [],
            "criteria_results": [{"criterion": "It works", "passed": True}],
            "proposed_tasks": [],
        }
        decision3 = evaluate_supervisor_status(repo=self.repo, report_override=verifier_report)
        self.assertEqual(decision3["decision"], "stop")
        self.assertEqual(decision3["reason"], "all_tasks_done")

        # Verify final state
        state = read_json(self.paths.state)
        self.assertTrue(state["state"]["completed"])
        self.assertEqual(state["state"]["accepts"], 1)
        self.assertEqual(state["state"]["reverts"], 0)
        self.assertEqual(state["state"]["active_tasks"], {})
        self.assertEqual(state["state"]["planner_runs"], 1)
        self.assertEqual(state["state"]["implementer_runs"], 1)
        self.assertEqual(state["state"]["verifier_runs"], 1)

        # Verify tasks.json shows task as done
        tasks = load_tasks(self.paths.tasks)
        self.assertEqual(tasks["tasks"][0]["status"], "done")

        # Verify events were recorded (header + init + planner + implementer + verifier = 5 lines)
        events_text = self.paths.events.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(events_text), 5)

        # Verify report files were persisted by report_override
        self.assertTrue((self.paths.reports / "planner-r001.json").exists())
        self.assertTrue((self.paths.reports / "impl-1-a1.json").exists())
        self.assertTrue((self.paths.reports / "verdict-1-a1.json").exists())


class TestRevertFlow(unittest.TestCase):
    """Verifier returns 'revert', state goes back to implementer for retry."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.repo = setup_repo(Path(self._tmpdir))
        initialize_run(
            repo=self.repo,
            goal="Revert test goal",
            scope=".",
            session_mode="background",
            execution_policy="danger_full_access",
            stop_condition="",
            allow_task_expansion="enabled",
            max_task_attempts=3,
            force=True,
        )
        self.paths = default_paths(self.repo)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_verifier_revert_sends_back_to_implementer(self) -> None:
        seed_task(self.repo, task_id="1", title="Revertible task")

        # Planner
        planner_report = {
            "role": "planner",
            "revision": 1,
            "summary": "Created task 1",
            "task_changes": {"added": ["1"], "updated": [], "closed": []},
            "planner_requested_reason": "initial_plan",
        }
        decision = evaluate_supervisor_status(repo=self.repo, report_override=planner_report)
        self.assertEqual(decision["reason"], "dispatch_implementer")

        # Implementer — create a real commit that can be reverted
        (self.repo / "bad.py").write_text("# bad implementation\n", encoding="utf-8")
        git(self.repo, "add", "bad.py")
        git(self.repo, "commit", "-m", "bad implementation")
        trial_commit = git(self.repo, "rev-parse", "--short", "HEAD")

        impl_report = {
            "role": "implementer",
            "task_id": "1",
            "attempt": 1,
            "commit": trial_commit,
            "summary": "Implemented badly",
            "files_changed": ["bad.py"],
            "checks_run": [],
            "proposed_tasks": [],
        }
        decision = evaluate_supervisor_status(repo=self.repo, report_override=impl_report)
        self.assertEqual(decision["reason"], "dispatch_verifier")

        # Verifier rejects
        verifier_report = {
            "role": "verifier",
            "task_id": "1",
            "attempt": 1,
            "commit": trial_commit,
            "verdict": "revert",
            "summary": "Implementation does not meet criteria",
            "findings": ["bad.py is incomplete"],
            "criteria_results": [{"criterion": "It works", "passed": False}],
            "proposed_tasks": [],
        }
        decision = evaluate_supervisor_status(repo=self.repo, report_override=verifier_report)
        self.assertEqual(decision["decision"], "relaunch")
        self.assertEqual(decision["reason"], "retry_task")

        # State should be back on implementer for the same task, attempt 2
        state = read_json(self.paths.state)
        self.assertEqual(state["state"]["active_tasks"]["1"]["role"], "implementer")
        self.assertEqual(state["state"]["active_tasks"]["1"]["attempt"], 2)
        self.assertEqual(state["state"]["reverts"], 1)
        self.assertEqual(state["state"]["active_tasks"]["1"]["trial_commit"], "")

        # The trial commit should have been reverted — HEAD should differ
        current_head = git(self.repo, "rev-parse", "--short", "HEAD")
        self.assertNotEqual(current_head, trial_commit)

    def test_revert_exhausts_max_attempts_triggers_replan(self) -> None:
        """After max_task_attempts reverts, the task fails and planner is invoked."""
        seed_task(self.repo, task_id="1", title="Difficult task")

        # Planner
        planner_report = {
            "role": "planner",
            "revision": 1,
            "summary": "Created task 1",
            "task_changes": {"added": ["1"], "updated": [], "closed": []},
            "planner_requested_reason": "initial_plan",
        }
        evaluate_supervisor_status(repo=self.repo, report_override=planner_report)

        # Run 3 implement-then-revert cycles (max_task_attempts=3)
        for attempt in range(1, 4):
            filename = f"attempt{attempt}.py"
            (self.repo / filename).write_text(f"# attempt {attempt}\n", encoding="utf-8")
            git(self.repo, "add", filename)
            git(self.repo, "commit", "-m", f"attempt {attempt}")
            trial_commit = git(self.repo, "rev-parse", "--short", "HEAD")

            impl_report = {
                "role": "implementer",
                "task_id": "1",
                "attempt": attempt,
                "commit": trial_commit,
                "summary": f"Attempt {attempt}",
                "files_changed": [filename],
                "checks_run": [],
                "proposed_tasks": [],
            }
            decision = evaluate_supervisor_status(repo=self.repo, report_override=impl_report)
            self.assertEqual(decision["reason"], "dispatch_verifier")

            verifier_report = {
                "role": "verifier",
                "task_id": "1",
                "attempt": attempt,
                "commit": trial_commit,
                "verdict": "revert",
                "summary": f"Attempt {attempt} rejected",
                "findings": [],
                "criteria_results": [],
                "proposed_tasks": [],
            }
            decision = evaluate_supervisor_status(repo=self.repo, report_override=verifier_report)

            if attempt < 3:
                self.assertEqual(decision["reason"], "retry_task")
            else:
                # Third revert should trigger replanning
                self.assertEqual(decision["decision"], "relaunch")
                self.assertEqual(decision["reason"], "planner_replan_after_revert")

        # Final state: planner should be current role, task should be failed
        state = read_json(self.paths.state)
        self.assertEqual(state["state"]["active_tasks"], {})
        self.assertEqual(state["state"]["planner_pending_reason"], "planner_replan_after_revert")
        self.assertEqual(state["state"]["reverts"], 3)

        tasks = load_tasks(self.paths.tasks)
        self.assertEqual(tasks["tasks"][0]["status"], "failed")


class TestServerManagerLifecycle(unittest.TestCase):
    """Verify ServerManager PID file creation and cleanup."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @patch.object(CodexAppServer, "start")
    def test_pid_file_created_on_acquire_and_cleaned_on_shutdown(self, mock_start: MagicMock) -> None:
        """Acquiring a server writes harness-servers.json; shutdown removes it."""
        pid_file = Path(self._tmpdir) / SERVERS_STATE_FILENAME

        # Mock the server so it reports a fake PID and looks alive
        mock_server = MagicMock(spec=CodexAppServer)
        mock_server.pid = 99999
        mock_server.alive = True

        manager = ServerManager(cwd=self._tmpdir)

        with patch.object(ServerManager, "_reap_idle"):
            with patch("harness_app_server.CodexAppServer", return_value=mock_server):
                ms = manager.acquire("task-1")

        # PID file should now exist with our fake PID
        self.assertTrue(pid_file.exists(), "harness-servers.json should exist after acquire")
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        self.assertIn(99999, data["pids"])

        # The ManagedServer should be assigned
        self.assertEqual(ms.current_task, "task-1")
        self.assertFalse(ms.idle)

        # Release makes it idle
        manager.release(ms)
        self.assertTrue(ms.idle)

        # Shutdown should remove the PID file
        manager.shutdown()
        self.assertFalse(pid_file.exists(), "harness-servers.json should be removed after shutdown")

    @patch.object(CodexAppServer, "start")
    def test_kill_orphans_cleans_stale_pid_file(self, mock_start: MagicMock) -> None:
        """kill_orphans reads and removes a leftover harness-servers.json."""
        pid_file = Path(self._tmpdir) / SERVERS_STATE_FILENAME

        # Write a fake PID file as if a previous run crashed
        pid_file.write_text(json.dumps({"pids": [999999]}), encoding="utf-8")
        self.assertTrue(pid_file.exists())

        manager = ServerManager(cwd=self._tmpdir)
        manager.kill_orphans()

        # PID file should be cleaned up (the fake PID won't exist, but no error)
        self.assertFalse(pid_file.exists(), "kill_orphans should remove stale PID file")

        # Shutdown to avoid atexit handler issues
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
