# Report Schemas

Role reports are the canonical handoff format between Codex turns.

Schemas are enforced at runtime via the `outputSchema` parameter on each `turn/start` request. The canonical schema definitions live in `schemas/*.schema.json` (one per role: `planner-report.schema.json`, `implementer-report.schema.json`, `verifier-report.schema.json`). The examples below are kept for quick reference.

## Planner Report

```json
{
  "role": "planner",
  "revision": 2,
  "summary": "Split auth work into storage, invalidation, and regression tasks.",
  "task_changes": {
    "added": ["T-001"],
    "updated": [],
    "closed": []
  },
  "planner_requested_reason": "initial_plan"
}
```

Compatibility note:
- the runtime also accepts `plan_revision` as an alias for `revision`

## Implementer Report

```json
{
  "role": "implementer",
  "task_id": "T-001",
  "attempt": 1,
  "commit": "abc1234",
  "summary": "Implemented auth session store and added focused unit coverage.",
  "files_changed": ["src/auth/store.ts", "tests/auth/store.test.ts"],
  "checks_run": ["python3 -m unittest"],
  "proposed_tasks": []
}
```

Compatibility note:
- the runtime also accepts `trial_commit` as an alias for `commit`

## Verifier Report

```json
{
  "role": "verifier",
  "task_id": "T-001",
  "attempt": 1,
  "commit": "abc1234",
  "verdict": "accept",
  "summary": "Acceptance criteria passed; regression checks are green.",
  "findings": [],
  "criteria_results": [
    {
      "criterion": "Acceptance criteria passed",
      "result": "pass",
      "evidence": "python3 -m unittest exited 0"
    }
  ],
  "proposed_tasks": []
}
```

Compatibility note:
- the runtime also accepts `evaluated_commit` as an alias for `commit`

## Proposal Convention

`proposed_tasks` should be a JSON array of objects with enough detail for the planner to convert them into canonical DAG tasks:

```json
[
  {
    "title": "Backfill auth cache invalidation tests",
    "reason": "Verifier found an uncovered invalidation edge case.",
    "depends_on": ["T-001"],
    "introduced_by": "verifier"
  }
]
```
