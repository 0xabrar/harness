from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_artifacts import read_json, write_json_atomic, write_tasks  # noqa: E402
from harness_build_prompt import build_planner_prompt  # noqa: E402
from harness_init_run import initialize_run  # noqa: E402
from harness_supervisor_status import evaluate_supervisor_status  # noqa: E402
from harness_artifacts import default_paths  # noqa: E402


class RoleFlowTests(unittest.TestCase):
    def test_planner_to_implementer_to_verifier_accept_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initialize_run(
                repo=tmp,
                goal="Build a harness",
                scope=".",
                session_mode="background",
                execution_policy="danger_full_access",
                force=True,
            )
            paths = default_paths(tmp)

            tasks = read_json(paths.tasks)
            tasks["planner_revision"] = 1
            tasks["tasks"] = [
                {
                    "id": "T-001",
                    "title": "Add harness runtime",
                    "description": "Implement the runtime skeleton.",
                    "acceptance_criteria": ["Runtime file exists", "CLI entrypoint exists"],
                    "status": "ready",
                    "priority": 1,
                    "dependencies": [],
                    "attempts": 0,
                }
            ]
            write_tasks(paths.tasks, tasks)
            planner_report = paths.reports / "planner-r001.json"
            write_json_atomic(
                planner_report,
                {
                    "role": "planner",
                    "revision": 1,
                    "summary": "Created the initial task DAG.",
                    "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
                },
            )
            planner_decision = evaluate_supervisor_status(repo=tmp)
            self.assertEqual(planner_decision["decision"], "relaunch")
            state = read_json(paths.state)
            self.assertEqual(state["state"]["current_role"], "implementer")
            self.assertEqual(state["state"]["current_task_id"], "T-001")

            implementer_report = paths.reports / "impl-T-001-a1.json"
            write_json_atomic(
                implementer_report,
                {
                    "role": "implementer",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": "abc1234",
                    "summary": "Built the runtime skeleton.",
                    "files_changed": ["scripts/harness_runtime_ctl.py"],
                    "checks_run": [],
                    "proposed_tasks": [],
                },
            )
            implementer_decision = evaluate_supervisor_status(repo=tmp)
            self.assertEqual(implementer_decision["reason"], "dispatch_verifier")
            state = read_json(paths.state)
            self.assertEqual(state["state"]["current_role"], "verifier")
            self.assertEqual(state["state"]["trial_commit"], "abc1234")

            verifier_report = paths.reports / "verdict-T-001-a1.json"
            write_json_atomic(
                verifier_report,
                {
                    "role": "verifier",
                    "task_id": "T-001",
                    "attempt": 1,
                    "commit": "abc1234",
                    "verdict": "accept",
                    "summary": "Acceptance criteria passed.",
                    "criteria_results": [{"criterion": "Runtime file exists", "passed": True}],
                    "proposed_tasks": [],
                },
            )
            verifier_decision = evaluate_supervisor_status(repo=tmp)
            self.assertEqual(verifier_decision["decision"], "stop")
            state = read_json(paths.state)
            self.assertTrue(state["state"]["completed"])

    def test_planner_prompt_mentions_canonical_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initialize_run(
                repo=tmp,
                goal="Build a harness",
                scope=".",
                session_mode="background",
                execution_policy="danger_full_access",
                force=True,
            )
            prompt = build_planner_prompt(default_paths(tmp))
            self.assertIn("tasks.json", prompt)
            self.assertIn("plan.md", prompt)
            self.assertIn("planner role", prompt.lower())


if __name__ == "__main__":
    unittest.main()
