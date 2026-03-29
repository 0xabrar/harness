# Runtime Control

The runtime control plane is a dumb script layer modeled after `codex-autoresearch`:

- launch
- resume
- status
- stop
- detached background execution
- fresh `codex exec` role turns
- artifact consistency checks
- `needs_human` transitions

It is **not** a fourth reasoning agent.

## Responsibilities

The runtime:

1. Persists `harness-launch.json`.
2. Initializes missing run-local artifacts.
3. Starts the detached process for background mode.
4. Chooses the next role from the current artifact state.
5. Builds the role prompt.
6. Launches a fresh `codex exec` session for that role.
7. Applies verifier verdicts (`accept` or `revert`).
8. Updates `harness-runtime.json`, `harness-state.json`, `harness-events.tsv`, and `harness-lessons.md`.
9. Transitions to `needs_human` if progress is unsafe or inconsistent.

## Non-Responsibilities

The runtime must not:

- invent new product tasks,
- edit product code,
- rewrite `tasks.json`,
- replace the planner,
- replace the verifier.

## Background Model

The detached runtime should:

1. Run with `stdin=DEVNULL`.
2. Append stdout/stderr to `harness-runtime.log`.
3. Persist pid/pgid and terminal reason in `harness-runtime.json`.
4. Relaunch fresh Codex turns until a terminal state is reached.

## Canonical Decisions

The runtime may emit only these high-level control decisions:

- `run_planner`
- `run_implementer`
- `run_verifier`
- `apply_accept`
- `apply_revert`
- `stop`
- `needs_human`

## Planner Interaction

The planner is the human-facing role before launch and the canonical owner of the task DAG throughout the run.

The runtime may schedule planner again when:

- the run has no plan yet,
- task proposals are pending,
- repeated verifier failures require replanning,
- no ready tasks remain,
- the planner explicitly requested another revision.
