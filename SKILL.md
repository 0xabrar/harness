---
name: harness
description: "Planner/implementer/verifier harness for long-running Codex work. Use when the user wants a dynamic task-DAG workflow with a human-facing planner, a coding implementer, a commit-level verifier, and a dumb runtime control plane that runs in the background and resumes from structured artifacts."
metadata:
  short-description: "Run a planner/implementer/verifier harness"
---

# Harness

Dynamic long-running Codex workflow with three agent roles and a dumb runtime control plane.

## Roles

- `planner`: user-facing before launch; owns `plan.md` and `tasks.json`; may add, split, reprioritize, and close tasks.
- `implementer`: writes product code for one ready task and creates a trial commit.
- `verifier`: evaluates the exact trial commit and returns `accept` or `revert`.
- `runtime`: not an LLM role; launches detached Codex turns, applies verifier verdicts, updates artifacts, and manages resume/status/stop.

## When Activated

1. Load [references/runtime-control.md](references/runtime-control.md) and [references/role-contracts.md](references/role-contracts.md).
2. Load [references/artifacts.md](references/artifacts.md) when creating, reading, or repairing state artifacts.
3. Load [references/state-machine.md](references/state-machine.md) before modifying runtime transitions or deciding the next role.
4. Load [references/report-schemas.md](references/report-schemas.md) when writing planner, implementer, or verifier reports.
5. Prefer the bundled helper scripts over hand-editing `harness-state.json`, `harness-events.tsv`, or `harness-lessons.md`.
6. Treat this as a file-mediated workflow. Do not invent live inter-agent conversations.

## Core Workflow

1. Planner creates or revises the task DAG.
2. Implementer works exactly one ready task and creates a trial commit.
3. Verifier evaluates that exact commit and returns `accept` or `revert`.
4. Runtime applies the verdict, updates state/logs, and decides the next role.
5. Repeat until the goal is complete or the runtime reaches `needs_human`.

## Hard Rules

1. The planner is the only role allowed to change task topology in `tasks.json` (add/split/reprioritize/close tasks).
2. The implementer is the only role allowed to write product code.
3. The verifier must evaluate the exact trial commit, not a mutable working tree.
4. The verifier returns only `accept`, `revert`, or `needs_human`.
5. The runtime may update execution-state fields for the current task (`in_progress`, `in_review`, `done`, `ready`, `blocked`) when applying verifier verdicts, but it does not invent product work or change DAG topology.
6. All role-to-role communication happens through artifacts in the target repo.
7. `needs_human` is a safety valve, not a normal step in the loop.
8. Use helper scripts for state/event/lessons updates whenever possible.
9. Foreground/manual same-session runs are unsupported. The harness must execute as a background runtime with fresh role turns.

## Run Modes

- `background`: persist launch/runtime artifacts and start the detached runtime controller.
- `status`: inspect the background runtime.
- `stop`: stop the background runtime.

## Artifacts

- `harness-launch.json`
- `harness-runtime.json`
- `harness-runtime.log`
- `harness-state.json`
- `harness-events.tsv`
- `harness-lessons.md`
- `tasks.json`
- `plan.md`
- `reports/*.json`

See [references/artifacts.md](references/artifacts.md) for schema and ownership rules.
