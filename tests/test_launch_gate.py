from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_artifacts import build_launch_manifest, default_paths, write_json_atomic  # noqa: E402
from harness_init_run import initialize_run  # noqa: E402
from harness_launch_gate import evaluate_launch_context  # noqa: E402


class LaunchGateTests(unittest.TestCase):
    def test_fresh_repo_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            decision = evaluate_launch_context(repo=tmp)
            self.assertEqual(decision["decision"], "fresh")

    def test_initialized_repo_with_launch_manifest_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initialize_run(
                repo=tmp,
                goal="Build a harness",
                scope=".",
                session_mode="foreground",
                execution_policy="danger_full_access",
                force=True,
            )
            paths = default_paths(tmp)
            write_json_atomic(
                paths.launch,
                build_launch_manifest(
                    original_goal="Build a harness",
                    prompt_text="Build a harness",
                    config={"goal": "Build a harness", "scope": ".", "session_mode": "background"},
                ),
            )
            decision = evaluate_launch_context(repo=tmp)
            self.assertEqual(decision["decision"], "resumable")


if __name__ == "__main__":
    unittest.main()

