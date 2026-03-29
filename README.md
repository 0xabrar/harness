# Harness

`harness` is a Codex-native skill and control plane for long-running coding work with explicit role separation:

- `planner`
- `implementer`
- `verifier`

Instead of a single agent looping on “try something, measure it, keep/discard it”, the harness runs a task-DAG workflow:

1. the planner creates or updates the task graph
2. the implementer works one ready task and creates a trial commit
3. the verifier evaluates that exact commit and returns `accept`, `revert`, or `needs_human`
4. the runtime control plane applies the verdict, updates artifacts, and launches the next fresh Codex turn

The runtime design intentionally mirrors the control-plane shape of `codex-autoresearch`:

- detached background runtime
- launch and runtime manifests
- append-only event log
- compact state snapshot
- lessons file
- resumable fresh-context turns

The workflow changes from metric optimization to task execution.

## Why This Exists

Most long-running agent systems need two things at once:

- clean role boundaries so planning, implementation, and verification do not collapse into one blurry loop
- a durable runtime layer that survives fresh sessions, background execution, and interruptions

This repo packages both:

- a skill entrypoint for Codex
- a file-mediated runtime protocol
- scripts for launch, status, stop, prompt generation, state updates, and lessons
- tests for the core control-plane transitions

## Architecture

### Roles

- `planner`
  - human-facing before launch
  - owns `plan.md` and `tasks.json`
  - may add, split, reprioritize, block, or close tasks
- `implementer`
  - writes product code for one ready task
  - creates exactly one trial commit for that task
- `verifier`
  - evaluates the exact trial commit
  - returns `accept`, `revert`, or `needs_human`
- `runtime`
  - not an LLM role
  - handles detached execution, state transitions, verdict application, status/stop, and artifact updates

### Core Loop

```text
planner -> implementer -> verifier -> runtime decision -> repeat
```

The runtime is intentionally dumb. It should not invent product work or rewrite the task graph. The planner owns topology, the implementer owns code changes, and the verifier owns judgment.

## Repository Layout

```text
harness/
├── SKILL.md
├── README.md
├── agents/openai.yaml
├── references/
├── scripts/
└── tests/
```

Important files:

- `SKILL.md`
  - skill entrypoint and role/rule summary
- `references/`
  - protocol docs, report schemas, state-machine docs, and artifact ownership
- `scripts/harness_runtime_ctl.py`
  - operator entrypoint for `create-launch`, `launch`, `start`, `run`, `status`, `stop`
- `scripts/harness_runtime_ops.py`
  - detached runtime lifecycle and fresh `codex exec` loop
- `scripts/harness_supervisor_status.py`
  - post-turn transition logic and verdict handling
- `scripts/harness_build_prompt.py`
  - role-specific prompt builder for planner, implementer, and verifier
- `scripts/harness_init_run.py`
  - initializes run-local artifacts
- `scripts/harness_lessons.py`
  - long-term lessons file handling

## Runtime Artifacts

The harness writes these files into the target repo:

- `harness-launch.json`
- `harness-runtime.json`
- `harness-runtime.log`
- `harness-state.json`
- `harness-events.tsv`
- `harness-lessons.md`
- `tasks.json`
- `plan.md`
- `reports/*.json`

Authority rules:

- `tasks.json` is the canonical task DAG
- `harness-state.json` is the canonical current snapshot
- `harness-events.tsv` is the append-only audit log
- `harness-runtime.log` is forensic trace output
- `harness-lessons.md` is durable strategic memory across turns

## Run Modes

- `foreground`
  - stay in the current Codex session
- `background`
  - persist launch/runtime artifacts and run detached in the background
- `status`
  - inspect a detached runtime
- `stop`
  - stop a detached runtime

## Installation

### Install as a local skill

Symlink the repo into your Codex skills directory:

```bash
mkdir -p ~/.codex/skills
ln -sfn /absolute/path/to/harness ~/.codex/skills/harness
```

Then start a fresh Codex session so the new skill is picked up.

### Run tests

```bash
python3 -m unittest discover -s tests -v
```

## Example

Use the skill the same way you would invoke a long-running Codex workflow:

```text
$harness
Build a Python notes CLI backed by a local JSON file. Use a planner/implementer/verifier flow and run it in background mode.
```

The planner should:

- scan the target repo
- create `plan.md` and `tasks.json`
- define explicit acceptance criteria
- keep the DAG coherent as the run evolves

The runtime should:

- launch fresh `codex exec` turns
- apply verifier decisions
- update state and lessons
- stop cleanly when the DAG is complete

## Current Scope

The harness is intentionally narrow:

- single-repo runs
- no live inter-agent conversation
- no swarm orchestration
- file-mediated coordination only
- accept/revert commit model

That constraint is deliberate. The goal is a robust long-running harness, not a maximal multi-agent framework.
