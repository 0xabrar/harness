#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from harness_artifacts import HarnessError, default_paths, utc_now


HEADER_RE = re.compile(r"^### L-(\d+):\s*(.+?)\s*$")
FIELD_RE = re.compile(r"^- \*\*(Category|Strategy|Outcome|Insight|Context|Iteration|Timestamp):\*\* (.+)$")
FIELD_MAP = {
    "Category": "category",
    "Strategy": "strategy",
    "Outcome": "outcome",
    "Insight": "insight",
    "Context": "context",
    "Iteration": "iteration",
    "Timestamp": "timestamp",
}
REQUIRED_FIELDS = ("category", "strategy", "outcome", "insight", "context", "iteration", "timestamp")


def parse_entries(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        header = HEADER_RE.match(line)
        if header:
            if current is not None:
                missing = [field for field in REQUIRED_FIELDS if field not in current]
                if missing:
                    raise HarnessError(f"Lesson {current['id']} is missing fields: {', '.join(missing)}")
                entries.append(current)
            current = {"id": f"L-{header.group(1)}", "title": header.group(2).strip()}
            continue
        if current is None:
            raise HarnessError("Lessons file contains content before a lesson header.")
        field = FIELD_RE.match(line)
        if field is None:
            raise HarnessError(f"Unparseable lesson line: {line!r}")
        current[FIELD_MAP[field.group(1)]] = field.group(2).strip()
    if current is not None:
        missing = [field for field in REQUIRED_FIELDS if field not in current]
        if missing:
            raise HarnessError(f"Lesson {current['id']} is missing fields: {', '.join(missing)}")
        entries.append(current)
    return entries


def append_lesson(*, path: Path, title: str, category: str, strategy: str, outcome: str, insight: str, context: str, iteration: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = parse_entries(path)
    number = len(entries) + 1
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing:
        existing += "\n"
    existing += "\n".join(
        [
            f"### L-{number}: {title.strip()}",
            f"- **Category:** {category.strip()}",
            f"- **Strategy:** {strategy.strip()}",
            f"- **Outcome:** {outcome.strip()}",
            f"- **Insight:** {insight.strip()}",
            f"- **Context:** {context.strip()}",
            f"- **Iteration:** {iteration.strip()}",
            f"- **Timestamp:** {utc_now()}",
            "",
        ]
    )
    path.write_text(existing, encoding="utf-8")
    return {"id": f"L-{number}", "title": title.strip(), "lessons_path": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Append or list harness lessons.")
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append")
    append.add_argument("--lessons-path", default=str(default_paths().lessons))
    append.add_argument("--title", required=True)
    append.add_argument("--category", required=True)
    append.add_argument("--strategy", required=True)
    append.add_argument("--outcome", required=True)
    append.add_argument("--insight", required=True)
    append.add_argument("--context", required=True)
    append.add_argument("--iteration", required=True)

    show = sub.add_parser("list")
    show.add_argument("--lessons-path", default=str(default_paths().lessons))

    args = parser.parse_args()
    if args.command == "append":
        result = append_lesson(
            path=Path(args.lessons_path),
            title=args.title,
            category=args.category,
            strategy=args.strategy,
            outcome=args.outcome,
            insight=args.insight,
            context=args.context,
            iteration=args.iteration,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(json.dumps(parse_entries(Path(args.lessons_path)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as exc:
        raise SystemExit(f"error: {exc}")

