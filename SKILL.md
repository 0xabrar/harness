---
name: harness
description: "Planner/implementer/verifier harness for long-running Codex work. Use when the user wants a dynamic task-DAG workflow with a human-facing planner, a coding implementer, a commit-level verifier, and a dumb runtime control plane that runs in the background and resumes from structured artifacts."
metadata:
  short-description: "Run a planner/implementer/verifier harness"
---

# Harness

Dynamic long-running Codex workflow with three agent roles and a dumb runtime control plane.

## Mode Detection

Detect the user's intent from their message after `$harness`:

| User says | Mode |
|---|---|
| `$harness <goal or description>` | **plan** |
| `$harness run` / `go` / `start` / `launch` | **run** |
| `$harness status` / `progress` / `check` | **status** |
| `$harness stop` / `halt` / `kill` | **stop** |

If ambiguous, ask which mode the user wants.

---

## Mode: Plan

The user provides a goal or description. You are the **planner role** in an interactive session.

### Steps

1. **Scan the repo.** Read the directory structure, key files, and any existing harness artifacts.
2. **Confirm the goal.** Restate it concisely and ask if it's correct.
3. **Propose scope and constraints.** Suggest what's in scope, what's out, and any stop conditions. Ask the user to confirm or adjust.
4. **Design the task DAG.** Break the goal into tasks with:
   - `id`, `title`, `description`
   - `acceptance_criteria` (explicit, testable)
   - `priority`, `dependencies`
   - `status` (set first tasks to `ready`, dependent ones to `pending`)
5. **Present the plan.** Show the task DAG to the user. Explain sequencing and any parallelism.
6. **Get approval.** Do not proceed until the user confirms.
7. **Write artifacts.** Once approved:
   - Write `plan.md` with the human-readable plan.
   - Write `tasks.json` with the canonical task DAG (must pass `validate_tasks_payload`).
   - Write `harness-launch.json` via the helper script:
     ```bash
     python3 scripts/harness_runtime_ctl.py create-launch \
       --repo <repo> \
       --original-goal "<user's original message>" \
       --goal "<confirmed goal>" \
       --scope "<confirmed scope>" \
       --stop-condition "<stop condition if any>" \
       --max-task-attempts 3
     ```
8. **Tell the user:** "Plan saved. Run `$harness run` to start."

### Planning Rules

- Every task must have explicit, testable acceptance criteria.
- Identify which tasks can run in parallel (no mutual dependencies).
- Keep tasks small — each should be one implementer turn.
- Do not write product code. Do not start the runtime.
- Do not create `harness-state.json` — the runtime initializes that on launch.

---

## Mode: Run

The user wants to launch the background runtime.

### Prerequisites

Check that `harness-launch.json` and `tasks.json` exist in the repo. If not, tell the user to run `$harness <goal>` first to create a plan.

### Launch

Run:
```bash
python3 scripts/harness_runtime_ctl.py start \
  --repo <repo> \
  --codex-bin codex
```

This initializes `harness-state.json` (if missing), spawns the detached background runtime, and returns the PID.

### After Launch

Report:
- The PID and log path.
- "The runtime is now running in the background."
- "Use `$harness status` to check progress."
- "Use `$harness stop` to halt."

### What the Runtime Does

The background runtime loops autonomously:
1. Reads `harness-state.json` to determine the current role.
2. Builds the role prompt.
3. Sends a turn to Codex via the app-server JSON-RPC protocol.
4. Receives the structured report via `outputSchema`.
5. Applies supervisor transitions (accept/revert/replan/dispatch next role).
6. Repeats until all tasks are done or it reaches `needs_human`.

If `tasks.json` already has `ready` tasks, the runtime skips the initial planner turn and goes directly to the implementer.

---

## Mode: Status

Run:
```bash
python3 scripts/harness_runtime_ctl.py status --repo <repo>
```

Present the result to the user. Highlight:
- Current status (running / stopped / needs_human / terminal)
- Last role and task
- Last decision and reason
- Whether the runtime process is still alive

Also show recent events from `harness-events.tsv` if available.

---

## Mode: Stop

Run:
```bash
python3 scripts/harness_runtime_ctl.py stop --repo <repo>
```

Confirm to the user that the runtime was stopped.

---

## Roles

- `planner`: user-facing before launch; owns `plan.md` and `tasks.json`; may add, split, reprioritize, and close tasks.
- `implementer`: writes product code for one ready task and creates a trial commit.
- `verifier`: evaluates the exact trial commit and returns `accept` or `revert`.
- `runtime`: not an LLM role; communicates with Codex via the app-server JSON-RPC protocol, applies verifier verdicts, updates artifacts, and manages resume/status/stop. When the planner produces multiple independent tasks, the runtime runs implementers concurrently.

## When Activated

1. **First: detect the mode** from the user's message (see Mode Detection above).
2. Load [references/runtime-control.md](references/runtime-control.md) and [references/role-contracts.md](references/role-contracts.md).
3. Load [references/artifacts.md](references/artifacts.md) when creating, reading, or repairing state artifacts.
4. Load [references/state-machine.md](references/state-machine.md) before modifying runtime transitions or deciding the next role.
5. Load [references/report-schemas.md](references/report-schemas.md) when writing planner, implementer, or verifier reports.
6. Prefer the bundled helper scripts over hand-editing `harness-state.json`, `harness-events.tsv`, or `harness-lessons.md`.
7. Treat this as a file-mediated workflow. Do not invent live inter-agent conversations.

## Core Workflow

1. User defines the plan interactively via `$harness <goal>` (plan mode).
2. User launches with `$harness run` (run mode).
3. Background runtime: implementer works a ready task → verifier evaluates → runtime applies verdict → repeat.
4. Runtime re-invokes the planner when: tasks need replanning, proposals are pending, or no ready tasks remain.
5. Loop ends when all tasks are done or the runtime reaches `needs_human`.

## Hard Rules

1. The planner is the only role allowed to change task topology in `tasks.json` (add/split/reprioritize/close tasks).
2. The implementer is the only role allowed to write product code.
3. The verifier must evaluate the exact trial commit, not a mutable working tree.
4. The verifier returns only `accept`, `revert`, or `needs_human`.
5. The runtime may update execution-state fields for the current task (`in_progress`, `in_review`, `done`, `ready`, `blocked`) when applying verifier verdicts, but it does not invent product work or change DAG topology.
6. All role-to-role communication happens through artifacts in the target repo.
7. `needs_human` is a safety valve, not a normal step in the loop.
8. Use helper scripts for state/event/lessons updates whenever possible.
9. The background runtime executes as fresh role turns via the app-server protocol. Foreground/manual same-session runs are unsupported.

## Artifacts

- `harness-launch.json`
- `harness-runtime.json`
- `harness-runtime.log`
- `harness-state.json`
- `harness-events.tsv`
- `harness-lessons.md`
- `tasks.json`
- `plan.md`
- `harness-servers.json` (app-server PID tracking for orphan cleanup)
- `reports/*.json`

See [references/artifacts.md](references/artifacts.md) for schema and ownership rules.
