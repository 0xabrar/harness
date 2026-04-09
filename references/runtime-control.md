# Runtime Control

The runtime control plane is a dumb script layer modeled after `codex-autoresearch`:

- launch
- resume
- status
- stop
- detached background execution
- app-server JSON-RPC role turns
- artifact consistency checks
- recovery transitions

It is **not** a fourth reasoning agent.

## Responsibilities

The runtime:

1. Persists `harness-launch.json`.
2. Initializes missing run-local artifacts.
3. Starts the detached process for background mode.
4. Chooses the next role from the current artifact state.
5. Builds the role prompt.
6. Sends a `turn/start` request to the app-server for that role (with the appropriate sandbox mode and `outputSchema`).
7. Applies verifier verdicts (`accept` or `revert`), including cherry-picking accepted task commits onto main and resetting rejected task worktrees for retry.
8. Updates `harness-runtime.json`, `harness-state.json`, `harness-events.tsv`, and `harness-lessons.md`.
9. Transitions into recovery if progress is unsafe or inconsistent.

Typical recovery cases are ambiguous acceptance criteria, environment blockers during verification, and integration failures while applying accepted task-local commits onto main.

## Non-Responsibilities

The runtime must not:

- invent new product tasks,
- edit product code,
- rewrite `tasks.json`,
- replace the planner,
- replace the verifier.

## App-Server Lifecycle (ServerManager)

The runtime manages Codex app-server processes through a `ServerManager`:

1. **Lazy spawn:** An app-server process is started on-demand when the first role turn is needed. The runtime does not pre-start servers.
2. **Idle reap:** Servers that have been idle for 5 minutes are automatically reaped to free resources.
3. **Signal/atexit cleanup:** On SIGTERM, SIGINT, or normal process exit, the runtime shuts down all managed servers.
4. **`harness-servers.json` orphan cleanup:** Each managed server's PID is recorded in `harness-servers.json` in the target repo. On startup, the runtime reads this file and kills any stale PIDs left by a previous crashed run before proceeding. The file is removed on clean shutdown.

## Parallel Execution

When the planner produces multiple independent tasks (i.e. tasks whose dependencies are all satisfied), the runtime runs implementer turns concurrently in isolated Git worktrees -- one task branch and one app-server turn per task. Accepted task commits are cherry-picked onto the main branch; rejected task commits stay in the task worktree and are reset for retry.

## Background Model

The detached runtime should:

1. Run with `stdin=DEVNULL`.
2. Append stdout/stderr to `harness-runtime.log`.
3. Persist pid/pgid and terminal reason in `harness-runtime.json`.
4. Send role turns via the app-server JSON-RPC protocol until a terminal state is reached.

## Canonical Decisions

The runtime may emit only these high-level control decisions:

- `run_planner`
- `run_implementer`
- `run_verifier`
- `apply_accept`
- `apply_revert`
- `stop`
- `recovery`

## Planner Interaction

The planner is the human-facing role before launch and the canonical owner of the task DAG throughout the run.

The runtime may schedule planner again when:

- the run has no plan yet,
- task proposals are pending,
- repeated verifier failures require replanning,
- no ready tasks remain,
- the planner explicitly requested another revision.
