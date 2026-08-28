# Test Design Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Design the failing test suite for the work described in `prompt`, from the plan in `previous_envelope`.

1. Read `<context_handoff_dir>/plan.md` — the planner's refined spec. Extract its concrete requirements.
2. Read `app.manifest.yaml` at the repo root for `app.generated_tests_dir` and `app.dir` (you will need the relative import path from the generated dir back to the app source).
3. Write ONE file: `<generated_tests_dir>/<adw_id>.test.ts`, where `<adw_id>` is the session directory name inside `context_handoff_dir` (`.../sessions/<adw_id>/context_handoff`). One test per requirement.
4. Prove it red, mechanically:
   - `bun x oxlint@1.36.0 <your file>` exits 0 — it parses.
   - `bun test <your file>` exits NON-zero — it fails on the tree as it stands.
5. Emit your `Report` JSON. `notes_for_next_agent` must carry the plan path (`<context_handoff_dir>/plan.md`) and the tail of the red `bun test` output — the builder's job is to make exactly that pass.

## Report

Respond with ONLY valid JSON matching `TestDesignOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence: how many tests, covering what>",
  "artifacts": ["<generated_tests_dir>/<adw_id>.test.ts"],
  "test_file": "<generated_tests_dir>/<adw_id>.test.ts",
  "cases": [
    {"name": "<the bun test name, verbatim>", "requirement": "<the plan requirement it proves, in the plan's words>"}
  ],
  "commit_message": "<imperative one-line git subject for committing THIS TEST FILE, not the feature it tests — e.g. 'Add red suite for the interval helper'>",
  "notes_for_next_agent": "<the plan path, plus the red bun test failure tail — what the builder must turn green>"
}
```

`test_file` is the path you ACTUALLY wrote. The gate opens it, lints it, and runs it — a name you meant to use fails all three.
