"""Unit tests for harness_schemas: schema loading and report validation."""

from __future__ import annotations

import unittest

from harness_schemas import load_schema, validate_report


class TestLoadSchema(unittest.TestCase):
    """Verify that each role schema loads and has the expected shape."""

    def test_load_planner_schema(self) -> None:
        schema = load_schema("planner")
        self.assertEqual(schema["type"], "object")
        self.assertIn("role", schema["properties"])
        self.assertEqual(schema["properties"]["role"]["const"], "planner")
        self.assertIn("revision", schema["required"])
        self.assertIn("task_changes", schema["required"])

    def test_load_implementer_schema(self) -> None:
        schema = load_schema("implementer")
        self.assertEqual(schema["type"], "object")
        self.assertIn("role", schema["properties"])
        self.assertEqual(schema["properties"]["role"]["const"], "implementer")
        self.assertIn("task_id", schema["required"])
        self.assertIn("files_changed", schema["required"])

    def test_load_verifier_schema(self) -> None:
        schema = load_schema("verifier")
        self.assertEqual(schema["type"], "object")
        self.assertIn("role", schema["properties"])
        self.assertEqual(schema["properties"]["role"]["const"], "verifier")
        self.assertIn("verdict", schema["required"])
        self.assertIn("criteria_results", schema["required"])
        self.assertEqual(
            schema["properties"]["verdict"]["enum"],
            ["accept", "revert", "needs_human"],
        )


class TestValidateReport(unittest.TestCase):
    """Verify structural validation of role reports."""

    def test_validate_valid_planner_report(self) -> None:
        report = {
            "role": "planner",
            "revision": 1,
            "summary": "Initial plan ready.",
            "task_changes": {"added": ["T-001"], "updated": [], "closed": []},
            "planner_requested_reason": "initial_plan",
        }
        self.assertTrue(validate_report(report, "planner"))

    def test_validate_valid_verifier_report(self) -> None:
        report = {
            "role": "verifier",
            "task_id": "T-001",
            "attempt": 1,
            "commit": "abc1234",
            "verdict": "accept",
            "summary": "All criteria met.",
            "findings": [],
            "criteria_results": [{"criterion": "tests pass", "passed": True}],
            "proposed_tasks": [],
        }
        self.assertTrue(validate_report(report, "verifier"))

    def test_validate_invalid_verdict_rejected(self) -> None:
        report = {
            "role": "verifier",
            "task_id": "T-001",
            "attempt": 1,
            "commit": "abc1234",
            "verdict": "maybe",
            "summary": "Uncertain.",
            "findings": [],
            "criteria_results": [],
            "proposed_tasks": [],
        }
        self.assertFalse(validate_report(report, "verifier"))

    def test_validate_missing_required_field_rejected(self) -> None:
        report = {
            "role": "planner",
            "revision": 1,
            # missing "summary", "task_changes", "planner_requested_reason"
        }
        self.assertFalse(validate_report(report, "planner"))


if __name__ == "__main__":
    unittest.main()
