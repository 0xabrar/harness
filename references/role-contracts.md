# Role Contracts

Each role returns its report as structured JSON as its final response. The runtime captures it via the app-server `outputSchema` mechanism.

## Sandbox Modes

- **planner** and **implementer**: `workspace-write` -- they need to modify files in the target repo.
- **verifier**: `read-only` -- it must not alter the commit under review.

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
- one implementer report for that attempt.

The implementer may:

- edit product code for the selected task,
- run local checks,
- create a single trial commit,
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
- produce `accept`, `revert`, or `needs_human`,
- propose follow-up tasks in its report.

The verifier must not:

- mutate `tasks.json`,
- write product code,
- silently change the commit under review.

## Runtime

The runtime owns:

- launch/runtime artifacts,
- process management,
- verdict application,
- state/events/lessons updates.

The runtime may update task execution-state fields after implementer/verifier turns, but it must not change task topology. It must stay deterministic and artifact-driven.

The runtime communicates with Codex exclusively through the app-server JSON-RPC protocol. It selects the sandbox mode per role (see above) and enforces report schemas via `outputSchema` on each `turn/start` request.
