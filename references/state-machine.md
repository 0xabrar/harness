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
verifying -> planning       (after replan request or new proposals)
verifying -> idle           (if no task is immediately ready)
idle -> completed           (all non-optional tasks done)
* -> recovery               (artifact inconsistency, repeated failure, hard blocker)
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

- `accept`: keep the task-local trial commit, cherry-pick it onto main, mark the task `done`, and clear task-local runtime state.
- `revert`: leave main untouched, reset the task worktree back to its base commit, requeue or block the task, and schedule planner if needed.

## Recovery

Enter recovery only for:

- invalid or inconsistent artifacts,
- repeated unchanged role exits,
- broken repo state that the runtime cannot repair safely,
- explicit recovery requests from verification.
