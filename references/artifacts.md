# Artifacts

The harness uses four memory layers:

1. Raw trace
2. Control-plane state
3. Run-local semantic state
4. Cross-run lessons

## Raw Trace

- `harness-runtime.log`
- optional `reports/*.json`

Purpose:
- debugging
- forensics
- post-run inspection

These files are not authoritative for resume.

## Control Plane

- `harness-launch.json`
- `harness-runtime.json`

Purpose:
- launch contract
- runtime status
- pid/pgid
- last control decision

## Run-Local Semantic State

- `tasks.json`
- `plan.md`
- `harness-state.json`
- `harness-events.tsv`
- `.harness-worktrees/` (ephemeral during parallel implementation)

Purpose:
- canonical DAG
- current role/task snapshot
- append-only audit trail
- isolated task workspaces while parallel implementers run

## Cross-Run Lessons

- `harness-lessons.md`

Purpose:
- reusable strategy memory
- planner/implementer/verifier lessons across runs

## Ownership

- planner owns `plan.md` and task-topology fields in `tasks.json`
- runtime owns `harness-launch.json`, `harness-runtime.json`, `harness-state.json`, `harness-events.tsv`, and `harness-lessons.md`
- runtime may update execution-state fields in `tasks.json` for the current task when applying verifier verdicts
- runtime owns `.harness-worktrees/` and may create/remove task branches and worktrees as part of isolated execution
- implementer and verifier own only their role reports under `reports/`

## Canonical Files

- `tasks.json` is the canonical task DAG.
- `harness-state.json` is the canonical current snapshot.
- `harness-events.tsv` is the append-only audit log.
- `harness-lessons.md` is the durable strategic memory.
- `harness-runtime.log` is a forensic trace only.
