from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_artifacts import load_tasks, report_path_for_role, write_tasks  # noqa: E402
from harness_artifacts import read_json  # noqa: E402
from harness_init_run import initialize_run  # noqa: E402
from harness_supervisor_status import evaluate_supervisor_status  # noqa: E402
from harness_task_worktree import prepare_task_worktree  # noqa: E402


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def setup_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "dev@example.com")
    git(repo, "config", "user.name", "Dev")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "base")
    return repo


def seed_ready_task(repo: Path) -> None:
    tasks = load_tasks(repo / "tasks.json")
    tasks["tasks"] = [
        {
            "id": "T-001",
            "title": "Add one line",
            "description": "Append one line to app.txt",
            "acceptance_criteria": ["app.txt contains one extra line"],
            "status": "ready",
            "priority": 1,
            "dependencies": [],
            "attempts": 0,
        }
    ]
    write_tasks(repo / "tasks.json", tasks)


class HarnessFlowTests(unittest.TestCase):
    def test_initialize_run_creates_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            result = initialize_run(
                repo=repo,
                goal="Build harness",
                scope="repo",
                session_mode="background",
                execution_policy="danger_full_access",
            )
            self.assertTrue(Path(result["state_path"]).exists())
            self.assertTrue(Path(result["events_path"]).exists())
            self.assertTrue(Path(result["tasks_path"]).exists())
            self.assertTrue(Path(result["plan_path"]).exists())
            events = (repo / "harness-events.tsv").read_text(encoding="utf-8")
            self.assertIn("initialize", events)

    def test_supervisor_accept_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            initialize_run(
                repo=repo,
                goal="Build harness",
                scope="repo",
                session_mode="background",
                execution_policy="danger_full_access",
            )
            seed_ready_task(repo)

            reports = type("Paths", (), {"reports": repo / "reports"})()
            planner_report = report_path_for_role(reports, "planner", planner_revision=1)
            write_json(
                planner_report,
                {
                    "role": "planner",
                    "revision": 1,
                    "summary": "Initial task graph ready.",
                    "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
                    "planner_requested_reason": "initial_plan",
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "relaunch")
            self.assertEqual(outcome["reason"], "dispatch_implementer")

            (repo / "app.txt").write_text("base\naccepted\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "implement task")
            trial_commit = git(repo, "rev-parse", "--short", "HEAD")
            impl_report = report_path_for_role(reports, "implementer", task_id="T-001", attempt=1)
            write_json(
                impl_report,
                {
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "summary": "Implemented the task.",
                    "files_changed": ["app.txt"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "relaunch")
            self.assertEqual(outcome["reason"], "dispatch_verifier")

            verdict_report = report_path_for_role(reports, "verifier", task_id="T-001", attempt=1)
            write_json(
                verdict_report,
                {
                    "role": "verifier",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "verdict": "accept",
                    "summary": "Task accepted.",
                    "criteria_results": [{"criterion": "app.txt contains one extra line", "passed": True}],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "stop")
            self.assertEqual(outcome["reason"], "all_tasks_done")
            state = read_json(repo / "harness-state.json")
            self.assertTrue(state["state"]["completed"])
            self.assertEqual({}, state["state"]["active_tasks"])
            self.assertEqual("all_tasks_done", state["state"]["last_decision"])

            events = (repo / "harness-events.tsv").read_text(encoding="utf-8").splitlines()
            self.assertIn("\tplanner\t-\t0\t-\tplan\tdispatch_implementer\t", events[2])
            self.assertIn("\tverifier\tT-001\t1\t", events[-1])

    def test_supervisor_revert_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            initialize_run(
                repo=repo,
                goal="Build harness",
                scope="repo",
                session_mode="background",
                execution_policy="danger_full_access",
            )
            seed_ready_task(repo)

            reports = type("Paths", (), {"reports": repo / "reports"})()
            planner_report = report_path_for_role(reports, "planner", planner_revision=1)
            write_json(
                planner_report,
                {
                    "role": "planner",
                    "revision": 1,
                    "summary": "Initial task graph ready.",
                    "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
                    "planner_requested_reason": "initial_plan",
                },
            )
            evaluate_supervisor_status(repo=repo)

            (repo / "app.txt").write_text("base\nrevert\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "implement task")
            trial_commit = git(repo, "rev-parse", "--short", "HEAD")
            impl_report = report_path_for_role(reports, "implementer", task_id="T-001", attempt=1)
            write_json(
                impl_report,
                {
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "summary": "Implemented the task badly.",
                    "files_changed": ["app.txt"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            evaluate_supervisor_status(repo=repo)

            verdict_report = report_path_for_role(reports, "verifier", task_id="T-001", attempt=1)
            write_json(
                verdict_report,
                {
                    "role": "verifier",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "verdict": "revert",
                    "summary": "Task rejected.",
                    "criteria_results": [{"criterion": "app.txt contains one extra line", "passed": False}],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "relaunch")
            self.assertEqual(outcome["reason"], "retry_task")
            self.assertNotEqual(git(repo, "rev-parse", "--short", "HEAD"), trial_commit)

    def test_verifier_recovery_request_counts_once_and_blocks_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            initialize_run(
                repo=repo,
                goal="Build harness",
                scope="repo",
                session_mode="background",
                execution_policy="danger_full_access",
            )
            seed_ready_task(repo)

            reports = type("Paths", (), {"reports": repo / "reports"})()
            planner_report = report_path_for_role(reports, "planner", planner_revision=1)
            write_json(
                planner_report,
                {
                    "role": "planner",
                    "revision": 1,
                    "summary": "Initial task graph ready.",
                    "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
                    "planner_requested_reason": "initial_plan",
                },
            )
            evaluate_supervisor_status(repo=repo)

            (repo / "app.txt").write_text("base\nblocked\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "implement task")
            trial_commit = git(repo, "rev-parse", "--short", "HEAD")
            impl_report = report_path_for_role(reports, "implementer", task_id="T-001", attempt=1)
            write_json(
                impl_report,
                {
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "trial_commit": trial_commit,
                    "summary": "Implemented the task, but verification should escalate.",
                    "files_changed": ["app.txt"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            evaluate_supervisor_status(repo=repo)

            verdict_report = report_path_for_role(reports, "verifier", task_id="T-001", attempt=1)
            write_json(
                verdict_report,
                {
                    "role": "verifier",
                    "task_id": "T-001",
                    "attempt": 1,
                    "evaluated_commit": trial_commit,
                    "verdict": "revert",
                    "recovery_signal": "environment_blocked",
                    "summary": "Manual decision required.",
                    "findings": [],
                    "criteria_results": [],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "recovery")
            self.assertEqual("environment_blocked", outcome["reason"])
            state = read_json(repo / "harness-state.json")
            self.assertEqual(1, state["state"]["recovery_requests"])
            self.assertEqual("recovery", state["state"]["last_status"])
            self.assertEqual("pending", state["state"]["recovery"]["status"])
            self.assertEqual("planner", state["state"]["recovery"]["owner"])
            self.assertEqual("environment_blocked", state["state"]["recovery"]["reason"])
            self.assertEqual("T-001", state["state"]["recovery"]["resume_task_id"])
            self.assertEqual({}, state["state"]["active_tasks"])
            tasks = read_json(repo / "tasks.json")
            self.assertEqual("blocked", tasks["tasks"][0]["status"])

    def test_worktree_recovery_request_keeps_main_branch_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            initialize_run(
                repo=repo,
                goal="Build harness",
                scope="repo",
                session_mode="background",
                execution_policy="danger_full_access",
            )
            seed_ready_task(repo)

            reports = type("Paths", (), {"reports": repo / "reports"})()
            planner_report = report_path_for_role(reports, "planner", planner_revision=1)
            write_json(
                planner_report,
                {
                    "role": "planner",
                    "revision": 1,
                    "summary": "Initial task graph ready.",
                    "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
                    "planner_requested_reason": "initial_plan",
                },
            )
            evaluate_supervisor_status(repo=repo)

            workspace = prepare_task_worktree(repo=repo, task_id="T-001")
            worktree = Path(workspace["worktree_path"])
            (worktree / "app.txt").write_text("base\nworktree\n", encoding="utf-8")
            git(worktree, "add", "app.txt")
            git(worktree, "commit", "-m", "implement in worktree")
            trial_commit = git(worktree, "rev-parse", "HEAD")
            main_head_before = git(repo, "rev-parse", "HEAD")

            state = read_json(repo / "harness-state.json")
            state["state"]["active_tasks"]["T-001"].update(workspace)
            write_json(repo / "harness-state.json", state)

            impl_report = report_path_for_role(reports, "implementer", task_id="T-001", attempt=1)
            write_json(
                impl_report,
                {
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "summary": "Implemented the task in an isolated worktree.",
                    "files_changed": ["app.txt"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            evaluate_supervisor_status(repo=repo)

            verdict_report = report_path_for_role(reports, "verifier", task_id="T-001", attempt=1)
            write_json(
                verdict_report,
                {
                    "role": "verifier",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "verdict": "revert",
                    "recovery_signal": "ambiguous_acceptance_criteria",
                    "summary": "Acceptance criteria are ambiguous.",
                    "findings": [],
                    "criteria_results": [],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "recovery")
            self.assertEqual("ambiguous_acceptance_criteria", outcome["reason"])

            state = read_json(repo / "harness-state.json")
            self.assertEqual(1, state["state"]["recovery_requests"])
            self.assertEqual("recovery", state["state"]["last_status"])
            self.assertEqual("pending", state["state"]["recovery"]["status"])
            self.assertEqual("planner", state["state"]["recovery"]["owner"])
            self.assertEqual("ambiguous_acceptance_criteria", state["state"]["recovery"]["reason"])
            self.assertEqual("T-001", state["state"]["recovery"]["resume_task_id"])
            self.assertEqual({}, state["state"]["active_tasks"])
            tasks = read_json(repo / "tasks.json")
            self.assertEqual("blocked", tasks["tasks"][0]["status"])
            self.assertEqual(main_head_before, git(repo, "rev-parse", "HEAD"))
            self.assertIn(str(worktree), git(repo, "worktree", "list"))

    def test_worktree_accept_conflict_routes_to_planner_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            initialize_run(
                repo=repo,
                goal="Build harness",
                scope="repo",
                session_mode="background",
                execution_policy="danger_full_access",
            )
            seed_ready_task(repo)

            reports = type("Paths", (), {"reports": repo / "reports"})()
            planner_report = report_path_for_role(reports, "planner", planner_revision=1)
            write_json(
                planner_report,
                {
                    "role": "planner",
                    "revision": 1,
                    "summary": "Initial task graph ready.",
                    "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
                    "planner_requested_reason": "initial_plan",
                },
            )
            evaluate_supervisor_status(repo=repo)

            workspace = prepare_task_worktree(repo=repo, task_id="T-001")
            worktree = Path(workspace["worktree_path"])
            (worktree / "app.txt").write_text("feature\n", encoding="utf-8")
            git(worktree, "add", "app.txt")
            git(worktree, "commit", "-m", "implement in worktree")
            trial_commit = git(worktree, "rev-parse", "HEAD")

            (repo / "app.txt").write_text("mainline\n", encoding="utf-8")
            git(repo, "commit", "-am", "mainline change")
            main_head_before = git(repo, "rev-parse", "HEAD")

            state = read_json(repo / "harness-state.json")
            state["state"]["active_tasks"]["T-001"].update(workspace)
            write_json(repo / "harness-state.json", state)

            impl_report = report_path_for_role(reports, "implementer", task_id="T-001", attempt=1)
            write_json(
                impl_report,
                {
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "summary": "Implemented the task in an isolated worktree.",
                    "files_changed": ["app.txt"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            evaluate_supervisor_status(repo=repo)

            verdict_report = report_path_for_role(reports, "verifier", task_id="T-001", attempt=1)
            write_json(
                verdict_report,
                {
                    "role": "verifier",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": trial_commit,
                    "verdict": "accept",
                    "summary": "Task accepted.",
                    "findings": [],
                    "criteria_results": [],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "recovery")
            self.assertEqual(outcome["reason"], "integration_conflict")

            state = read_json(repo / "harness-state.json")
            self.assertEqual("recovery", state["state"]["last_status"])
            self.assertEqual("integration_conflict", state["state"]["last_decision"])
            self.assertEqual(1, state["state"]["recovery_requests"])
            self.assertEqual("pending", state["state"]["recovery"]["status"])
            self.assertEqual("planner", state["state"]["recovery"]["owner"])
            self.assertEqual("planner", state["state"]["recovery"]["incident"]["owner"])
            self.assertEqual("integration_conflict", state["state"]["recovery"]["incident"]["reason"])
            self.assertEqual("T-001", state["state"]["recovery"]["incident"]["resume_task_id"])
            self.assertEqual(1, state["state"]["recovery"]["incident"]["resume_attempt"])
            self.assertEqual(trial_commit, state["state"]["recovery"]["incident"]["commit"])
            self.assertEqual("conflict", state["state"]["recovery"]["incident"]["details"]["outcome"])
            self.assertEqual(str(worktree), state["state"]["recovery"]["incident"]["details"]["worktree_path"])
            self.assertEqual({}, state["state"]["active_tasks"])

            tasks = read_json(repo / "tasks.json")
            self.assertEqual("blocked", tasks["tasks"][0]["status"])
            self.assertEqual("accept", tasks["tasks"][0]["last_verdict"])
            self.assertEqual(main_head_before, git(repo, "rev-parse", "HEAD"))
            self.assertIn(str(worktree), git(repo, "worktree", "list"))

    def test_report_alias_fields_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            initialize_run(
                repo=repo,
                goal="Build harness",
                scope="repo",
                session_mode="background",
                execution_policy="danger_full_access",
            )
            seed_ready_task(repo)

            reports = type("Paths", (), {"reports": repo / "reports"})()
            planner_report = report_path_for_role(reports, "planner", planner_revision=1)
            write_json(
                planner_report,
                {
                    "role": "planner",
                    "plan_revision": 1,
                    "summary": "Initial task graph ready.",
                    "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
                    "planner_requested_reason": "initial_plan",
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["reason"], "dispatch_implementer")

            (repo / "app.txt").write_text("base\nalias\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "implement task")
            trial_commit = git(repo, "rev-parse", "--short", "HEAD")
            impl_report = report_path_for_role(reports, "implementer", task_id="T-001", attempt=1)
            write_json(
                impl_report,
                {
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "trial_commit": trial_commit,
                    "summary": "Implemented the task.",
                    "files_changed": ["app.txt"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["reason"], "dispatch_verifier")

            verdict_report = report_path_for_role(reports, "verifier", task_id="T-001", attempt=1)
            write_json(
                verdict_report,
                {
                    "role": "verifier",
                    "task_id": "T-001",
                    "attempt": 1,
                    "evaluated_commit": trial_commit,
                    "verdict": "accept",
                    "summary": "Task accepted.",
                    "criteria_results": [],
                    "proposed_tasks": [],
                },
            )
            outcome = evaluate_supervisor_status(repo=repo)
            self.assertEqual(outcome["decision"], "stop")


if __name__ == "__main__":
    unittest.main()
