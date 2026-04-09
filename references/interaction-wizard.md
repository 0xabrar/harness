# Interaction Wizard

For new launches:

1. Scan the repo.
2. Confirm the high-level goal in plain English.
3. Propose defaults for:
   - scope
   - stop condition
   - whether the planner may expand the task graph
4. Present a short confirmation summary.
5. Require an explicit `go` before launch.

The planner is the user-facing intelligence for launch preparation. After launch approval, the harness runs autonomously in the background until it completes or an unrecoverable runtime fault leaves it paused in recovery.
