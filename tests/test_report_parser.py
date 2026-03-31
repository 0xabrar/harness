from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_report_parser import parse_structured_output  # noqa: E402


class TestParseStructuredOutput(unittest.TestCase):
    def test_parse_valid_json(self) -> None:
        raw = '{"role": "planner", "revision": 1}'
        result = parse_structured_output(raw)
        self.assertEqual(result["parsed"], {"role": "planner", "revision": 1})
        self.assertIsNone(result["parse_error"])
        self.assertEqual(result["raw"], raw)

    def test_parse_empty_returns_error(self) -> None:
        result = parse_structured_output("")
        self.assertIsNone(result["parsed"])
        self.assertEqual(result["parse_error"], "Empty output from Codex turn.")
        self.assertEqual(result["raw"], "")

    def test_parse_invalid_json_returns_error(self) -> None:
        raw = "this is not json at all"
        result = parse_structured_output(raw)
        self.assertIsNone(result["parsed"])
        self.assertIn("Could not parse JSON", result["parse_error"])
        self.assertEqual(result["raw"], raw)

    def test_parse_json_in_markdown_fence(self) -> None:
        raw = 'Some preamble text\n```json\n{"verdict": "accept", "summary": "ok"}\n```\ntrailing text'
        result = parse_structured_output(raw)
        self.assertEqual(result["parsed"], {"verdict": "accept", "summary": "ok"})
        self.assertIsNone(result["parse_error"])
        self.assertEqual(result["raw"], raw)

    def test_parse_none_returns_error(self) -> None:
        result = parse_structured_output(None)
        self.assertIsNone(result["parsed"])
        self.assertEqual(result["parse_error"], "Empty output from Codex turn.")
        self.assertEqual(result["raw"], "")


if __name__ == "__main__":
    unittest.main()
