# Verifier Protocol

The verifier:

1. Evaluates the exact trial commit named in the implementer report.
2. Checks task acceptance criteria.
3. Runs any required tests or validation commands.
4. Writes a verdict report with:
   - `accept` or `revert`
   - findings
   - criteria results
   - optional proposed follow-up tasks

The verifier does not modify `tasks.json`, does not cherry-pick onto main, and does not apply the retry/reset itself.
