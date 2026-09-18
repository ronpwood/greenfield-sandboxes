#!/usr/bin/env python3
"""Per-agent tool-call and error metrics from a pulled traces directory.

`just sbx manage traces <run-id>` brings a run's THINKING home; this reads it and
answers one question: which agents are fighting their tools?

That number is the reason this exists. On gf-e2e-20260917-cbb166 the builder made
128 tool calls and 20 of them errored — 18 of those were the SAME schema mistake
(`edit` called with `edits:[...]` and no `path`). The builder's own thinking
misdiagnosed it as "large edits fail, small ones succeed", so it split its edits
and then fell back to 32 whole-file `write` calls. Build plus revise cost 12.0M
tokens. None of that was visible in the run log, the envelopes or the cost
report: it only shows up by counting `tool_execution_end` events. A prompt fix
for a failure you cannot count is a guess, so count it first, change the prompt
second, and measure the same number again.

Reads `<traces-dir>/sessions/<adw_id>/<agent>/raw_output.jsonl`, the raw pi event
stream. Never touches a VM, and never reads a live run's data — traces are pulled
first, on purpose, so a metric can be recomputed later from bytes that are not
moving under it.

Error kinds are classified from the tool's own message text, not guessed:

    model-format  the model emitted malformed structured output
    schema        "Validation failed for tool ..."  the CALL was malformed
    not-found     "Could not find the exact text"   oldText did not match the file
    no-op         "No changes made"                 the edit changed nothing
    other         anything else, printed so it can be classified later

`model-format` is split out from `schema` because THEY HAVE DIFFERENT OWNERS. A
schema error is the model misusing a tool it was told how to use — a prompt can
fix that, and the 2026-09-18 tool-contract change cut those from 18 to 0-2 per
arm. A model-format error is the model's own function-calling serialization
breaking: on gf2-1, deepseek leaked its native markup into a JSON argument value
and invented a tool named `content` out of the same mangled parse. No prompt
fixes that; it is a provider bug. Folding the two together makes a prompt change
look less effective than it was, and sends a provider bug to the wrong owner.

Usage:
    trace_metrics.py <traces-dir> [--json]
    trace_metrics.py .sandbox/traces/gf-e2e-20260917-cbb166

Stdlib only, like run_record.py: this is host tooling that must run before any
project toolchain exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The event that means "a tool call finished". tool_execution_start is not used:
# a call that never ends is not a call that errored, and counting starts would
# double-count against ends on a killed run.
END_EVENT = "tool_execution_end"

# Ordered longest-lived first; the first match wins, so keep these mutually
# exclusive in practice and let anything unrecognised fall through to "other"
# rather than being silently folded into a neighbouring kind.
# ORDER MATTERS: model-format is checked FIRST, because its symptoms arrive
# through the ordinary schema validator — a corrupted argument is still reported
# as "Validation failed for tool ...". Checking schema first swallows every one.
ERROR_KINDS = (
    ("model-format", ("DSML", "Tool content not found")),
    ("schema", ("Validation failed for tool",)),
    ("not-found", ("Could not find the exact text", "Could not find edits[")),
    ("no-op", ("No changes made",)),
)

# A tool name the registry never defined means the model hallucinated it, which
# is the same serialization failure wearing a different hat.
KNOWN_TOOLS = {"bash", "read", "write", "edit", "ls", "grep", "find", "glob",
               "subagent_create", "subagent_continue", "subagent_list",
               "subagent_remove"}


def classify(text: str, tool: str = "") -> str:
    # An unknown tool name is a model-format failure whatever the message says:
    # the registry is fixed, so the model invented the name.
    if tool and tool not in KNOWN_TOOLS and tool != "<unnamed>":
        return "model-format"
    for kind, needles in ERROR_KINDS:
        if any(n in text for n in needles):
            return kind
    return "other"


def error_text(event: dict) -> str:
    """The tool's message, flattened. Shape: result.content[].text."""
    result = event.get("result")
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def scan_agent(path: Path) -> dict:
    """One agent's raw_output.jsonl -> its counts.

    A malformed line is COUNTED in `skipped`, never dropped quietly: a truncated
    trace (rsync mid-write, a killed run) would otherwise read as a clean, low
    error rate, which is the one wrong answer this script must not give.
    """
    calls = 0
    errors = 0
    skipped = 0
    per_tool: dict[str, int] = {}
    per_tool_errors: dict[str, int] = {}
    per_kind: dict[str, int] = {}
    samples: dict[str, str] = {}

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                skipped += 1
                continue
            if not isinstance(event, dict) or event.get("type") != END_EVENT:
                continue

            name = event.get("toolName") or "<unnamed>"
            calls += 1
            per_tool[name] = per_tool.get(name, 0) + 1
            if not event.get("isError"):
                continue

            errors += 1
            per_tool_errors[name] = per_tool_errors.get(name, 0) + 1
            kind = classify(error_text(event), name)
            per_kind[kind] = per_kind.get(kind, 0) + 1
            # Keep one example per kind: a bare count of "other" is not
            # actionable, and the first line of the message usually names it.
            if kind not in samples:
                first = error_text(event).strip().splitlines()
                samples[kind] = first[0][:120] if first else ""

    return {
        "calls": calls,
        "errors": errors,
        "rate": (errors / calls) if calls else 0.0,
        "skipped": skipped,
        "per_tool": per_tool,
        "per_tool_errors": per_tool_errors,
        "per_kind": per_kind,
        "samples": samples,
    }


def collect(traces_dir: Path) -> list[dict]:
    """Every (adw_id, agent) under <traces-dir>/sessions/, sorted."""
    sessions = traces_dir / "sessions"
    if not sessions.is_dir():
        raise SystemExit(
            f"trace_metrics: no sessions/ under {traces_dir}\n"
            f"  is this a traces dir? pull one with: just sbx manage traces <run-id>"
        )

    rows = []
    for adw_dir in sorted(p for p in sessions.iterdir() if p.is_dir()):
        for agent_dir in sorted(p for p in adw_dir.iterdir() if p.is_dir()):
            raw = agent_dir / "raw_output.jsonl"
            if not raw.is_file():
                continue  # pi_sessions/, prompts/, context_handoff/ etc.
            row = {"adw_id": adw_dir.name, "agent": agent_dir.name}
            row.update(scan_agent(raw))
            rows.append(row)
    return rows


def fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "—"
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def print_table(rows: list[dict], traces_dir: Path) -> None:
    print(f"trace metrics — {traces_dir}")
    if not rows:
        print("  no raw_output.jsonl under sessions/ — nothing to measure")
        return

    head = f"{'adw':10s} {'agent':14s} {'calls':>6s} {'errs':>5s} {'rate':>6s}  {'by tool':28s} by kind"
    print()
    print(head)
    print("-" * len(head))
    for r in rows:
        print(
            f"{r['adw_id'][:10]:10s} {r['agent'][:14]:14s} "
            f"{r['calls']:6d} {r['errors']:5d} {r['rate'] * 100:5.1f}%  "
            f"{fmt_counts(r['per_tool_errors'])[:28]:28s} {fmt_counts(r['per_kind'])}"
        )

    tot_calls = sum(r["calls"] for r in rows)
    tot_errs = sum(r["errors"] for r in rows)
    tot_skip = sum(r["skipped"] for r in rows)
    print("-" * len(head))
    rate = (tot_errs / tot_calls * 100) if tot_calls else 0.0
    print(f"{'TOTAL':10s} {'':14s} {tot_calls:6d} {tot_errs:5d} {rate:5.1f}%")

    # Examples last: the table answers "how bad", these answer "bad at what".
    seen: dict[str, str] = {}
    for r in rows:
        for kind, sample in r["samples"].items():
            seen.setdefault(f"{r['agent']}/{kind}", sample)
    if seen:
        print()
        print("first error of each kind:")
        for key, sample in sorted(seen.items()):
            print(f"  {key:26s} {sample}")

    if tot_skip:
        print()
        print(f"!! {tot_skip} unparseable line(s) skipped — the trace may be truncated,")
        print("   so treat these counts as a floor. Re-pull: just sbx manage traces <run-id>")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--json"]
    as_json = "--json" in argv[1:]
    if len(args) != 1:
        print(__doc__.strip().split("Usage:")[-1].strip(), file=sys.stderr)
        return 2

    traces_dir = Path(args[0])
    if not traces_dir.is_dir():
        print(f"trace_metrics: no such directory: {traces_dir}", file=sys.stderr)
        return 1

    rows = collect(traces_dir)
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print_table(rows, traces_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
