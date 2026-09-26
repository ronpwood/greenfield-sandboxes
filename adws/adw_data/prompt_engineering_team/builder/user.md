# Build Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Your part in this run: make the team's spec true for the person who asked for the work in `prompt`.

1. Read `<context_handoff_dir>/plan.md`, starting with `## What we're solving for`, then
   `previous_envelope`: it holds the red suite, the test failures, or the reviewer's findings you
   are answering now.
2. Build. Where the spec is wrong on first principles, propose an amendment in `plan.md` and build
   the correct thing.
3. Check it: the grading command, your own sweep of every `V` row, and a look at any output channel
   you changed.
4. Leave a signed note under `## Team notes` for anything the reviewer should know, then emit your
   `Report` JSON.

## Report

Respond with ONLY valid JSON matching `BuildOutput` — no prose before or after:

```json
{
  "status": "success",
  "summary": "<one sentence describing what you built>",
  "changed_files": ["src/server.ts"],
  "artifacts": [],
  "checks": [
    {"claim": "<what is true of the build>", "command": "<the command or /tmp script that showed it>", "result": "<what it printed, abridged — never just 'passed'>"}
  ],
  "departures": ["<spec item> — <what you built instead> — <why, from first principles>"],
  "open_questions": ["<what you could not verify, or are unsure of>"],
  "commit_message": "<imperative one-line git subject for the code you changed — this is what the commit of your work will say>",
  "notes_for_next_agent": "<what the reviewer should look at hardest, and how to verify it>"
}
```

`departures` and `open_questions` may be empty lists when there are none; `checks` should not be.
