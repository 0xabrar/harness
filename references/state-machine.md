# State Machine

The harness is a role-driven loop over a dynamic task DAG.

## Runtime States

- `planning`
- `implementing`
- `verifying`
- `idle`
- `completed`
- `stopped`
- `needs_human`

## High-Level Transitions

```text
idle -> planning
planning -> implementing
implementing -> verifying
verifying -> implementing   (after revert + retry)
verifying -> planning       (after replan request or new proposals)
verifying -> idle           (if no task is immediately ready)
idle -> completed           (all non-optional tasks done)
* -> needs_human            (artifact inconsistency, repeated failure, hard blocker)
* -> stopped                (user stop)
```

## Replan Triggers

Schedule planner when:

- there is no canonical plan yet,
- task proposals are pending,
- a task has exceeded retry thresholds,
- accepted work created new dependencies,
- no ready tasks remain but unfinished tasks still exist.

## Accept/Revert Semantics

- `accept`: keep the trial commit, mark the task `done`, clear trial state.
- `revert`: revert the trial commit, clear trial state, requeue or block the task, and schedule planner if needed.

## `needs_human`

Enter `needs_human` only for:

- invalid or inconsistent artifacts,
- repeated unchanged role exits,
- broken repo state that the runtime cannot repair safely,
- explicit verifier escalation.
