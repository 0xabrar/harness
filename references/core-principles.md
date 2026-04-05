# Core Principles

1. Keep the control plane dumb and deterministic.
2. Keep the planner, implementer, and verifier as separate fresh-context turns.
3. Use files as the source of truth.
4. Prefer resumability over cleverness.
5. Prefer simple commit semantics: implementer commits in an isolated task worktree, verifier accepts or rejects that task-local commit, and the runtime cherry-picks only accepted work onto main.
6. Preserve a clean-state invariant after every accepted step.
7. Use lessons as strategic memory, not as the source of truth for the current run.
