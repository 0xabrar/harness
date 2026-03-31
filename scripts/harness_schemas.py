"""Load and validate JSON schemas for harness role reports.

Provides ``load_schema`` to retrieve the raw JSON schema for a role and
``validate_report`` to perform lightweight structural validation using
only the Python 3.11+ standard library (no ``jsonschema`` dependency).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMAS_DIR: Path = Path(__file__).resolve().parent.parent / "schemas"

_SCHEMA_FILES: dict[str, str] = {
    "planner": "planner-report.schema.json",
    "implementer": "implementer-report.schema.json",
    "verifier": "verifier-report.schema.json",
}

# Python type-name mapping for JSON Schema "type" values.
_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def load_schema(role: str) -> dict[str, Any]:
    """Return the parsed JSON schema for *role*.

    Raises ``ValueError`` if the role is unknown and ``FileNotFoundError``
    if the schema file is missing on disk.
    """
    filename = _SCHEMA_FILES.get(role)
    if filename is None:
        raise ValueError(f"Unknown role: {role!r}. Expected one of {sorted(_SCHEMA_FILES)}")
    path = SCHEMAS_DIR / filename
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate_report(report: dict[str, Any], role: str) -> bool:
    """Validate *report* against the schema for *role*.

    Performs basic structural checks without requiring the ``jsonschema``
    package:

    * All ``required`` fields are present.
    * ``const`` constraints match exactly.
    * ``enum`` constraints are satisfied.
    * Top-level and nested ``type`` constraints are checked.
    * ``additionalProperties: false`` is enforced at the top level.

    Returns ``True`` when the report is valid, ``False`` otherwise.
    """
    try:
        schema = load_schema(role)
    except (ValueError, FileNotFoundError):
        return False

    return _validate_object(report, schema)


def _validate_object(obj: Any, schema: dict[str, Any]) -> bool:
    """Recursively validate *obj* against an object-level *schema*."""

    # -- type check ----------------------------------------------------------
    expected_type = schema.get("type")
    if expected_type is not None:
        py_type = _JSON_TYPE_MAP.get(expected_type)
        if py_type is not None and not isinstance(obj, py_type):
            return False

    if not isinstance(obj, dict):
        # For non-object schemas we only needed the type check above.
        return True

    # -- required fields -----------------------------------------------------
    for field in schema.get("required", []):
        if field not in obj:
            return False

    properties: dict[str, Any] = schema.get("properties", {})

    # -- additionalProperties ------------------------------------------------
    if schema.get("additionalProperties") is False:
        allowed = set(properties)
        if not set(obj).issubset(allowed):
            return False

    # -- per-property checks -------------------------------------------------
    for key, prop_schema in properties.items():
        if key not in obj:
            continue
        value = obj[key]

        # const
        if "const" in prop_schema and value != prop_schema["const"]:
            return False

        # enum
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            return False

        # type
        prop_type = prop_schema.get("type")
        if prop_type is not None:
            py_type = _JSON_TYPE_MAP.get(prop_type)
            if py_type is not None and not isinstance(value, py_type):
                return False

        # Recurse into nested objects
        if prop_type == "object" and isinstance(value, dict):
            if not _validate_object(value, prop_schema):
                return False

    return True
