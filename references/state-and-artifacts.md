# State And Artifacts

The harness uses four layers of memory:

1. Raw trace
   - `harness-runtime.log`
2. Control plane
   - `harness-launch.json`
   - `harness-runtime.json`
3. Run-local working memory
   - `harness-state.json`
   - `harness-events.tsv`
   - `plan.md`
   - `tasks.json`
   - `reports/`
4. Cross-run strategic memory
   - `harness-lessons.md`

Authority rules:

- `tasks.json` is the canonical task graph.
- `harness-state.json` is the canonical runtime snapshot.
- `harness-state.json` and `harness-runtime.json` both carry recovery metadata for resume decisions.
- `harness-events.tsv` is the append-only audit log.
- `harness-lessons.md` is strategic cross-run memory.
- `harness-runtime.log` is forensic trace only.
