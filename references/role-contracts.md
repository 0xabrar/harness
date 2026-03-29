# Role Contracts

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
