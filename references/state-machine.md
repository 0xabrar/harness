# State Machine

The harness is a role-driven loop over a dynamic task DAG.

## Runtime States

- `planning`
- `implementing`
- `verifying`
- `idle`
- `recovery`
- `completed`
- `terminal`

## High-Level Transitions

```text
idle -> planning
planning -> implementing
implementing -> verifying
verifying -> implementing   (after revert + retry)
verifying -> planning       (after verifier recovery, integration conflict, replan request, or new proposals)
verifying -> idle           (if no task is immediately ready)
recovery -> planning        (planner-owned follow-up resumes the live run)
recovery -> terminal        (runtime-owned fault or user stop)
idle -> completed           (all non-optional tasks done)
* -> recovery               (planner-owned incident or runtime-owned fault)
* -> terminal               (all tasks done or user stop)
```

## Replan Triggers

Schedule planner when:

- there is no canonical plan yet,
- task proposals are pending,
- a task has exceeded retry thresholds,
- accepted work created new dependencies,
- no ready tasks remain but unfinished tasks still exist.

## Accept/Revert Semantics

- `accept`: keep the task-local trial commit, cherry-pick it onto main, mark the task `done`, clear task-local runtime state, and refresh readiness. Stop only if the refreshed DAG has no unfinished work.
- `revert`: leave main untouched, reset the task worktree back to its base commit, requeue or block the task, and schedule planner if needed.

## Recovery

Planner-owned recovery keeps the same run alive. Record the incident, hand control back to planner, and continue once the planner clarifies or repairs the DAG.

Runtime-owned recovery pauses the run for later resume. Enter that state only for:

- invalid or inconsistent artifacts,
- repeated unchanged role exits,
- broken repo state that the runtime cannot repair safely,
- launch/bootstrap faults that prevent safe continuation.

`needs_human` is legacy compatibility only; new transitions use structured `recovery` metadata in `harness-state.json` and `harness-runtime.json`.
