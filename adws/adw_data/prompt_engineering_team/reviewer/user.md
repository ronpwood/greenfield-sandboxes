# Review Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Your part in this run: decide whether the person who asked for the work in `prompt` got what they needed.

1. Establish the effective spec: read `<context_handoff_dir>/plan.md` if it exists (the frozen
   sections plus accepted amendments), else use `prompt`. Read `## What we're solving for` first.
2. Rule on every amendment marked `proposed`, in `plan.md`, with a first-principles reason.
3. Read the code that was actually written, starting from `previous_envelope.changed_files`.
4. Write and run `<context_handoff_dir>/value_sweep.<ext>` over every effective `V` row, against the
   delivered code.
5. Rule on every requirement: one `findings` entry each, with evidence.
6. Write the review to `<context_handoff_dir>/review.md`, add a signed team note if you learned
   something the team should keep, then emit your `Report` JSON.

## Report

Respond with ONLY valid JSON matching `ReviewOutput` — no prose before or after:

```json
{
  "status": "success",
  "approved": false,
  "summary": "<one sentence: N of M requirements met, K of L values met>",
  "findings": [
    { "requirement": "<the ask, in the requester's words>", "met": true, "evidence": "src/server.ts:42 — handler registered" }
  ],
  "value_checks": [
    { "id": "V1", "input": "<as in the spec>", "expected": "<as in the spec>", "actual": "<what your sweep printed>", "met": true }
  ],
  "blocking": ["<what must change before this can be approved>"],
  "artifacts": ["<context_handoff_dir>/review.md", "<context_handoff_dir>/value_sweep.<ext>"],
  "notes_for_next_agent": "<what the builder must fix, or how to verify if approved>"
}
```

`status` is `success` when the review itself completed; it is not the verdict. The verdict is `approved`, and it is true only when `findings` has no unmet entry, `value_checks` has no unmet entry, and `blocking` is empty.
