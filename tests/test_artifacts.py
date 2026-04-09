from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_artifacts import (  # noqa: E402
    build_runtime_payload,
    build_state_payload,
    default_paths,
    default_recovery_payload,
    normalize_recovery_payload,
    normalize_runtime_payload,
    normalize_state_payload,
)


class RecoveryPayloadTests(unittest.TestCase):
    def test_default_recovery_payload_includes_nested_defaults(self) -> None:
        recovery = default_recovery_payload()

        self.assertEqual("clear", recovery["status"])
        self.assertEqual("", recovery["incident"]["owner"])
        self.assertEqual("", recovery["incident"]["reason"])
        self.assertEqual({}, recovery["incident"]["details"])
        self.assertEqual(0, recovery["retry"]["count"])
        self.assertEqual("", recovery["retry"]["reason"])

    def test_normalize_recovery_payload_backfills_legacy_planner_incident(self) -> None:
        normalized = normalize_recovery_payload(
            {
                "status": "pending",
                "owner": "planner",
                "reason": "verifier_escalated",
                "resume_role": "planner",
                "resume_task_id": "T-001",
                "resume_attempt": "2",
            }
        )

        self.assertEqual("pending", normalized["status"])
        self.assertEqual("planner", normalized["incident"]["owner"])
        self.assertEqual("verifier_escalated", normalized["incident"]["reason"])
        self.assertEqual("planner", normalized["incident"]["resume_role"])
        self.assertEqual("T-001", normalized["incident"]["resume_task_id"])
        self.assertEqual(2, normalized["incident"]["resume_attempt"])
        self.assertEqual(0, normalized["retry"]["count"])
        self.assertEqual("", normalized["retry"]["reason"])

    def test_normalize_state_payload_preserves_status_and_round_trips_planner_incident(self) -> None:
        state_payload = build_state_payload(config={"goal": "Build harness"})
        state_payload["state"]["last_status"] = "recovery"
        state_payload["state"]["recovery"] = {
            "status": "pending",
            "incident": {
                "owner": "planner",
                "reason": "integration_conflict",
                "resume_role": "planner",
                "resume_task_id": "T-009",
                "resume_attempt": 3,
                "commit": "abc1234",
                "details": {"outcome": "conflict"},
            },
        }

        normalized = normalize_state_payload(json.loads(json.dumps(state_payload)))

        self.assertEqual("recovery", normalized["state"]["last_status"])
        self.assertEqual("pending", normalized["state"]["recovery"]["status"])
        self.assertEqual("planner", normalized["state"]["recovery"]["owner"])
        self.assertEqual("planner", normalized["state"]["recovery"]["incident"]["owner"])
        self.assertEqual("integration_conflict", normalized["state"]["recovery"]["incident"]["reason"])
        self.assertEqual("T-009", normalized["state"]["recovery"]["incident"]["resume_task_id"])
        self.assertEqual(3, normalized["state"]["recovery"]["incident"]["resume_attempt"])
        self.assertEqual("abc1234", normalized["state"]["recovery"]["incident"]["commit"])
        self.assertEqual({"outcome": "conflict"}, normalized["state"]["recovery"]["incident"]["details"])

    def test_normalize_runtime_payload_backfills_legacy_runtime_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_runtime_payload(
                paths=default_paths(Path(tmp)),
                status="recovery",
                terminal_reason="app_server_retryable",
            )
            payload["recovery"] = {
                "status": "pending",
                "owner": "runtime",
                "reason": "app_server_retryable",
                "resume_role": "implementer",
                "resume_task_id": "T-004",
                "resume_attempt": "2",
            }

            normalized = normalize_runtime_payload(json.loads(json.dumps(payload)))

        self.assertEqual("recovery", normalized["status"])
        self.assertEqual("pending", normalized["recovery"]["status"])
        self.assertEqual("runtime", normalized["recovery"]["owner"])
        self.assertEqual("app_server_retryable", normalized["recovery"]["retry"]["reason"])
        self.assertEqual("implementer", normalized["recovery"]["retry"]["resume_role"])
        self.assertEqual("T-004", normalized["recovery"]["retry"]["resume_task_id"])
        self.assertEqual(2, normalized["recovery"]["retry"]["resume_attempt"])
        self.assertEqual(0, normalized["recovery"]["retry"]["count"])
        self.assertEqual("", normalized["recovery"]["incident"]["owner"])

    def test_normalize_runtime_payload_round_trips_runtime_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_runtime_payload(
                paths=default_paths(Path(tmp)),
                status="recovery",
                terminal_reason="runtime_retry",
            )
            payload["recovery"] = {
                "status": "pending",
                "retry": {
                    "count": 2,
                    "reason": "git_cherry_pick_conflict",
                    "resume_role": "runtime",
                    "resume_task_id": "T-010",
                    "resume_attempt": 1,
                },
            }

            normalized = normalize_runtime_payload(json.loads(json.dumps(payload)))

        self.assertEqual("recovery", normalized["status"])
        self.assertEqual("pending", normalized["recovery"]["status"])
        self.assertEqual("runtime", normalized["recovery"]["owner"])
        self.assertEqual(2, normalized["recovery"]["retry"]["count"])
        self.assertEqual("git_cherry_pick_conflict", normalized["recovery"]["retry"]["reason"])
        self.assertEqual("runtime", normalized["recovery"]["retry"]["resume_role"])
        self.assertEqual("T-010", normalized["recovery"]["retry"]["resume_task_id"])
        self.assertEqual(1, normalized["recovery"]["retry"]["resume_attempt"])
        self.assertEqual("", normalized["recovery"]["incident"]["owner"])


if __name__ == "__main__":
    unittest.main()
