#!/usr/bin/env python3
"""Parse structured output from app-server turn results."""
from __future__ import annotations

import json
import re
from typing import Any


def parse_structured_output(raw: str | None) -> dict[str, Any]:
    """Extract a JSON dict from raw turn output.

    Handles:
    - None or empty string
    - Valid JSON string
    - JSON wrapped in markdown ```json ... ``` fences
    - Invalid / unparseable JSON
    """
    if raw is None or raw.strip() == "":
        return {"parsed": None, "parse_error": "Empty output from Codex turn.", "raw": ""}

    text = raw.strip()

    # Try extracting JSON from markdown fences first.
    fence_match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    candidate = fence_match.group(1).strip() if fence_match else text

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return {
            "parsed": None,
            "parse_error": f"Could not parse JSON from model output. Raw length={len(raw)}",
            "raw": raw,
        }

    if not isinstance(data, dict):
        return {
            "parsed": None,
            "parse_error": f"Could not parse JSON from model output. Raw length={len(raw)}",
            "raw": raw,
        }

    return {"parsed": data, "parse_error": None, "raw": raw}
