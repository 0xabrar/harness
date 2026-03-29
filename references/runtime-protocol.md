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
4. runtime applies verdict and decides the next role

Planner reruns when:

- the run is fresh
- there are no ready tasks
- a verifier rejection suggests missing prerequisite work
- implementer/verifier reports propose new tasks
- the same task has repeated rejections

