# Planner Protocol

The planner owns:

- `plan.md`
- `tasks.json`
- planner reports under `reports/`

Responsibilities:

1. Translate the high-level goal into a task DAG.
2. Define acceptance criteria per task.
3. Split oversized tasks.
4. Add newly discovered tasks when justified by implementer or verifier reports.
5. Reprioritize and unblock the DAG.

The planner does not write product code.

