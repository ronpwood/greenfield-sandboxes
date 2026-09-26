# Test Design Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Your part in this run: make the team's spec executable before the build exists, as a suite that fails now and passes only when the work described in `prompt` is right.

1. Read `<context_handoff_dir>/plan.md`, the team's spec, starting with `## What we're solving for`. Every `R` and every `V` row is yours to turn into assertions.
2. Read `app.manifest.yaml` at the repo root for `app.generated_tests_dir` and `app.dir` (you will need the relative import path from the generated dir back to the app source).
3. Write ONE file: `<generated_tests_dir>/<adw_id>.test.ts`, where `<adw_id>` is the session directory name inside `context_handoff_dir` (`.../sessions/<adw_id>/context_handoff`). At least one test per `R`, one assertion per `V` row, expected values copied from the table.
4. Prove it red, mechanically:
   - `bun x oxlint@1.36.0 <your file>` exits 0 — it parses.
   - `bun test <app.test_file> <your file>`, the grading command: your tests appear and FAIL, and the fixed suite's still PASS, read per file.
5. If a row is untestable or looks wrong, propose an amendment in `plan.md` (never edit the frozen sections in place), and leave a signed team note for the builder.
6. Emit your `Report` JSON. `notes_for_next_agent` must carry the plan path (`<context_handoff_dir>/plan.md`) and the tail of the red `bun test` output — the builder's job is to make exactly that pass.

## Report

Respond with ONLY valid JSON matching `TestDesignOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence: how many tests, covering what>",
  "artifacts": ["<generated_tests_dir>/<adw_id>.test.ts"],
  "test_file": "<generated_tests_dir>/<adw_id>.test.ts",
  "cases": [
    {"name": "<the bun test name, verbatim>", "requirement": "<the spec entry it proves, in the spec's words>", "spec_ids": ["R2", "V4"]}
  ],
  "commit_message": "<imperative one-line git subject for committing THIS TEST FILE, not the feature it tests — e.g. 'Add red suite for the interval helper'>",
  "notes_for_next_agent": "<the plan path, plus the red bun test failure tail — what the builder must turn green>"
}
```

`test_file` is the path you ACTUALLY wrote. The gate opens it, lints it, and runs it — a name you meant to use fails all three.
