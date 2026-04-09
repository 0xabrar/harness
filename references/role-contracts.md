# Role Contracts

Each role returns its report as structured JSON as its final response. The runtime captures it via the app-server `outputSchema` mechanism.

## Sandbox Modes

- **planner** and **implementer**: `workspace-write` when the environment supports bwrap, otherwise `danger-full-access`.
- **verifier**: `read-only` under `workspace_write`, otherwise `danger-full-access` when the environment cannot support the restricted sandbox.

## Planner

The planner owns:

- `plan.md`
- `tasks.json`

The planner may:

- add tasks,
- split tasks,
- reprioritize tasks,
- close tasks,
- mark tasks blocked,
- convert task proposals into canonical tasks.

The planner must not:

- write product code,
- create product commits,
- directly apply verifier verdicts.

## Implementer

The implementer owns:

- one task attempt at a time,
- one trial commit at a time,
- one isolated task worktree/branch when parallel execution is active,
- one implementer report for that attempt.

The implementer may:

- edit product code for the selected task,
- run local checks,
- create a single trial commit,
- work inside its assigned task worktree,
- propose new tasks in its report.

The implementer must not:

- mutate `tasks.json`,
- accept or revert its own commit,
- continue to a second task in the same turn.

### Thread Resume for Retries

When a verifier reverts an implementer's attempt, the runtime resumes the same app-server thread (via `thread_id`) so the implementer retains context from its previous attempt. The verifier's feedback is prepended to the resumed prompt.

## Verifier

The verifier evaluates:

- the exact trial commit,
- the task acceptance criteria,
- relevant checks and reports.

The verifier may:

- run deterministic verification,
- inspect changed files,
- produce `accept` or `revert`,
- surface ambiguous verification as recovery context for the runtime,
- propose follow-up tasks in its report.

The verifier must not:

- mutate `tasks.json`,
- write product code,
- silently change the commit under review or the main branch.

## Runtime

The runtime owns:

- launch/runtime artifacts,
- process management,
- verdict application,
- task worktree/branch lifecycle,
- state/events/lessons updates.

The runtime may update task execution-state fields after implementer/verifier turns, cherry-pick accepted commits onto main, and reset/remove task worktrees, but it must not change task topology. It must stay deterministic and artifact-driven.

The runtime communicates with Codex exclusively through the app-server JSON-RPC protocol. It selects the sandbox mode per role (see above) and enforces report schemas via `outputSchema` on each `turn/start` request.
