from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_init_run import initialize_run  # noqa: E402
from harness_artifacts import read_json  # noqa: E402


class HarnessInitTests(unittest.TestCase):
    def test_initialize_run_creates_expected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            result = initialize_run(
                repo=repo,
                goal="Build a harness",
                scope=".",
                session_mode="foreground",
                execution_policy="danger_full_access",
                force=True,
            )

            self.assertTrue(Path(result["state_path"]).exists())
            self.assertTrue(Path(result["events_path"]).exists())
            self.assertTrue(Path(result["tasks_path"]).exists())
            self.assertTrue(Path(result["plan_path"]).exists())

            state = read_json(Path(result["state_path"]))
            self.assertEqual(state["mode"], "harness")
            self.assertEqual(state["config"]["goal"], "Build a harness")
            self.assertEqual(state["state"]["current_role"], "planner")
            self.assertEqual(state["state"]["seq"], 1)

            events = Path(result["events_path"]).read_text(encoding="utf-8")
            self.assertIn("seq\ttimestamp\trole", events)
            self.assertIn("Initialized harness state and working artifacts.", events)


if __name__ == "__main__":
    unittest.main()

