# Development Guide

This file documents gotchas, constraints, and internal knowledge for developing this package. It is for contributors modifying the harness code, not for end users.

## App-Server Protocol

### Thread Lifecycle

- `thread/start` requires `cwd`, `approvalPolicy`, and `sandbox` as params. Omitting them produces cryptic errors like "invalid thread id: invalid length: expected length 32".
- The response from `thread/start` is `response.thread.id`, NOT `response.threadId`. Same for `thread/resume`.
- One app-server process can host many threads, but only one active turn at a time. To run parallel turns, spawn multiple app-server processes.
- Threads are separate context windows. A planner thread and an implementer thread share no conversation history.

### Notifications

- Notifications arrive as `item/started` and `item/completed`, not as type-specific methods.
- The item type is in `params.item.type` (e.g., `"agentMessage"`, `"fileChange"`, `"commandExecution"`, `"reasoning"`).
- The assistant's final response text is in `params.item.text` on `agentMessage` items. Capture the last one — that's your structured output.
- `turn/completed` signals the end. It does NOT contain the response text — that comes from prior `item/completed` notifications.
- `error` notifications can arrive before `turn/completed`. Check `params.error` for details. Common: rate limits, schema validation errors.
- Filter `turn/completed` by `params.threadId` to distinguish your thread's completion from subagent completions.

### Sandbox Modes

- `"read-only"` — can read files and run commands but cannot write. Uses bwrap.
- `"workspace-write"` — can read/write within the repo. Uses bwrap.
- `"danger-full-access"` — no sandbox at all. Equivalent to `--dangerously-bypass-approvals-and-sandbox` from `codex exec`.
- bwrap fails on some environments (containers, VMs) with `bwrap: loopback: Failed RTM_NEWADDR`. If you hit this, use `danger-full-access`.
- Sandbox mode is set per-thread at `thread/start` time, not per-turn.

### Initialize Handshake

- After spawning `codex app-server`, you must send `initialize` (request with id) then `initialized` (notification without id).
- The `capabilities.experimentalApi` field should be `false` unless you know you need it.
- You can opt out of noisy streaming notifications via `capabilities.optOutNotificationMethods`.

## outputSchema (Structured Output)

### Strict Mode Requirements

The Codex API uses OpenAI's strict structured output mode. Every schema must follow these rules:

1. **Every object must have `additionalProperties: false`.** This includes the top-level object AND every nested object (e.g., items inside arrays).
2. **Every object must have `required` listing ALL its properties.** You cannot have optional fields — list every property in `required`.
3. **Every array must have `items` defined.** Bare `{"type": "array"}` is rejected. You need `{"type": "array", "items": {"type": "string"}}` or a full object schema.
4. **Nested objects in array items need full definitions.** `{"type": "array", "items": {"type": "object"}}` is rejected. You must define `properties`, `required`, and `additionalProperties: false` on the items object.

If any of these rules are violated, the app-server returns an `error` notification with `invalid_json_schema` and the turn fails with empty output.

### Schema Location

Schemas live in `schemas/*.schema.json`. They are loaded by `scripts/harness_schemas.py` and passed as `outputSchema` to `turn/start`. If you add a new role or change report fields, update the schema file.

### Debugging Schema Errors

When a turn returns empty `final_message`, check the `error` field in the turn result. The error message from the API tells you exactly which context path violated the rules (e.g., `In context=('properties', 'proposed_tasks', 'items')`).

## ServerManager

### Process Lifecycle

- Servers are spawned lazily on first `acquire()`. There is no pre-warming.
- Idle servers (no active task for 5 minutes) are reaped on the next `acquire()` call.
- All servers are killed on `shutdown()`, which is registered via `atexit` and signal handlers (SIGTERM, SIGINT).
- PIDs are written to `harness-servers.json` for orphan recovery after crashes. `kill_orphans()` reads this file on startup.

### Concurrency

- `ServerManager` is thread-safe (uses `threading.Lock`).
- Each parallel implementer gets its own `ManagedServer` from the pool.
- `ManagedServer.thread_history` maps task IDs to thread IDs, enabling thread resume for retries. This is per-server — if the server that hosted a thread dies, the thread is gone and a fresh one is started.

## Supervisor and State

### report_override

- `evaluate_supervisor_status` accepts an optional `report_override` parameter.
- When provided, it uses the report directly instead of reading from disk.
- The report is always written to disk regardless (for auditability and so events/lessons can reference it).
- When processing parallel implementer results, set `current_task_id` and `current_attempt` in `harness-state.json` before each `evaluate_supervisor_status` call.

### State Machine Single-Slot Limitation

- `harness-state.json` has one `current_task_id` slot. It was designed for sequential execution.
- Parallel results must be processed sequentially through the supervisor. Before each call, update the state file to point at the correct task.
- This is a known limitation, not a bug. A proper multi-task state model would be a larger redesign.

## Testing

- Unit tests mock `CodexAppServer` — they don't spawn real processes.
- Integration tests in `test_integration.py` use real git repos in temp directories.
- Run all tests: `cd /path/to/harness && PYTHONPATH=scripts python3 -m pytest tests/ -v`
- The test for sandbox values (`TestSandboxForRole`) must be updated if you change `ROLE_SANDBOX` in `harness_runtime_ops.py`.
