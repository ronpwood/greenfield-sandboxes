# Appendix: how the tools and the shell behave

These are the mechanics. Every line is here because a real run lost time to it.

## Shell

- You inherit the operator's shell environment: their PATH, toolchains and credentials are already live. Call tools by bare name (`bun`, `uv`, `git`, `pytest`). Never hunt for a binary or fall back to an absolute `/usr/bin/*` path.
- Judge a command by its exit status, never by scanning its output for words. `error` or `not found` inside passing output is text, not a failure. (One exception, the grading command, is explained where it applies.)
- Send scratch output to `/tmp`, never into the repo. A redirect like `bun test > out.txt` inside the working tree is an out-of-scope write and will be undone.
- A command that never exits (a foreground server, an uncleared `setInterval`) hangs your turn. Run servers in the background, and give probes an explicit exit or a `timeout`.

## Tool contracts

- **Every file tool needs `path`, every time.** `edit`, `write`, `read` — all of them, on every call, including for a file you just read, just wrote, or just edited. This is the single most common tool failure there is: it accounted for 18 of one build's 20 tool errors, and it has since been observed on `edit` and on `write`, from builders and reviewers alike. Before you send a file tool call, check that `path` is in it.
- **Get the argument shapes right.** `edit` takes `{path, edits: [{oldText, newText}, ...]}`, and a flat `{path, oldText, newText}` is accepted for a single replacement. Every entry inside `edits` needs **both** `oldText` and `newText` — a missing one fails with `edits.N.oldText: must have required properties oldText`. `write` takes `{path, content}`. `read` takes `path`, and its optional `limit`/`offset` are **numbers, not strings**.
- **The error text names the cause — read it and fix that.** `Validation failed for tool …` is a malformed CALL, never an edit that was too large: resend the same edit with the argument fixed, and do not shrink it, split it, or switch tools in response. `Could not find the exact text` (or `Could not find edits[i]`) means `oldText` does not match — re-`read` and copy it from the file. `No changes made … identical content` means `oldText` and `newText` were the same, so the edit was a no-op.
- **Every `oldText` is matched against the original file, not against your earlier edits.** Within one `edit` call, write all `oldText` values as the file looks now. Across separate calls the file has already changed, so `read` it again before editing the same region twice.
- **Edits in one call must not overlap or nest — this is a hard constraint, not advice.** Two entries whose `oldText` touch the same lines fail with `edits[i] and edits[j] overlap`. When two changes are near each other, **merge them into one larger entry** that spans both; do not send them as neighbours. Splitting an overlap into separate calls also works but costs a round trip and reintroduces the stale-text problem.
- **Put several changes to one file in one `edit` call**, as multiple entries in `edits`. That is cheaper than one call per change and avoids the stale-text problem entirely.
- **Each `oldText` must be unique in the file.** `Found N occurrences … The text must be unique` means add surrounding context, not retry.
- **Use `edit` for existing files and `write` (`{path, content}`) only for new files or a deliberate full rewrite.** Falling back to whole-file `write` after an edit error rewrites code you never meant to touch and hides the real problem.
