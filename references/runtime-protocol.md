# Runtime Protocol

The runtime control plane is not a reasoning agent. It is responsible for:

- launch manifest creation
- detached runtime start/stop
- status inspection
- resume safety checks
- prompting the correct role next
- applying verifier verdicts
- updating state, events, and lessons

Role order:

1. `planner` on a fresh run
2. `implementer` for the next ready task
3. `verifier` for the implementer's exact trial commit
4. runtime applies the verdict, refreshes readiness/recovery state, and decides the next role

An `accept` does not imply an immediate stop. The runtime integrates the accepted commit, refreshes the DAG, and either dispatches more work, re-runs planner, or stops only if all tasks are done.

Planner-owned recovery is part of the same live loop. The runtime records recovery context, hands control back to planner, and continues if the planner can safely clarify or repair the DAG. Only launch/runtime faults that cannot be repaired in-process should leave the runtime paused in `recovery`.

Planner reruns when:

- the run is fresh
- there are no ready tasks
- verifier recovery signals require clarification or repair work
- accepted work exposed an integration conflict that needs a repair task
- runtime retries exhausted and the planner must unblock the task graph
- a verifier rejection suggests missing prerequisite work
- implementer/verifier reports propose new tasks
- the same task has repeated rejections
