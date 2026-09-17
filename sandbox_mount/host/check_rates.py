#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare the rates we ship against OpenRouter's live catalog.

Same failure class as the toolchain lock: a table that is right on the day it is
written, goes stale silently, and only announces itself as a number nobody
trusts. `toolchain.lock` + gate F solved that for tool versions; this is the
price equivalent.

It matters more than a reporting nicety. On 2026-09-07 seven of eleven rates
were wrong, in BOTH directions -- gemini-3.6-flash 2.0x high, gpt-5.6-luna 2.0x
low -- so every ADW cost line, and the 2026-08-22 fan-out table built from them,
was wrong by a per-model factor. A best-of-N that ranks arms on cost was ranking
on fiction. The run record's `spend` (what the disposable key actually billed)
was right all along; only the estimate was broken.

    uv run sandbox_mount/host/check_rates.py            # report, exit 1 on drift
    uv run sandbox_mount/host/check_rates.py --fix      # rewrite the template

Reads sandbox_mount/guest/models.json.tmpl. The template carries `{{...}}`
placeholders, so it is not valid JSON on disk -- they are stubbed before parsing
and the file is rewritten by targeted substitution, never re-serialized, so the
placeholders and the file's formatting survive.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

CATALOG = "https://openrouter.ai/api/v1/models"
TEMPLATE = Path(__file__).resolve().parents[1] / "guest" / "models.json.tmpl"

# pi's cost keys -> OpenRouter's pricing keys. Both are per-token in the API and
# per-MILLION tokens in our template, hence the 1e6.
FIELDS = (("input", "prompt"),
          ("output", "completion"),
          ("cacheRead", "input_cache_read"),
          ("cacheWrite", "input_cache_write"))

# A model block's cost object, captured with its id so we can match them up.
BLOCK = re.compile(r'("id":\s*"([^"]+)".*?"cost":\s*\{)([^}]*)(\})', re.S)


def live_rates() -> dict[str, dict[str, float]]:
    with urllib.request.urlopen(CATALOG, timeout=30) as response:
        catalog = json.loads(response.read())
    rates = {}
    for model in catalog.get("data", []):
        pricing = model.get("pricing") or {}
        rates[model["id"]] = {
            ours: float(pricing.get(theirs) or 0) * 1e6 for ours, theirs in FIELDS
        }
    return rates


def fmt(value: float) -> str:
    """Match the template's existing style: trimmed, but never a bare integer."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if "." in text else f"{text}.0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true",
                        help="rewrite the template with the live rates")
    args = parser.parse_args()

    text = TEMPLATE.read_text()
    # Prove it still parses before and after; a template we corrupt is worse
    # than one that is merely stale.
    json.loads(re.sub(r"\{\{[^}]*\}\}", "PLACEHOLDER", text))
    rates = live_rates()

    drift: list[tuple[str, str, str]] = []
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        head, model_id, body, tail = match.groups()
        if model_id not in rates:
            missing.append(model_id)
            return match.group(0)
        current = {k: v for k, v in re.findall(r'"(\w+)":\s*([0-9.]+)', body)}
        wanted = {ours: fmt(rates[model_id][ours]) for ours, _ in FIELDS}
        if any(float(current.get(k, 0)) != float(v) for k, v in wanted.items()):
            drift.append((model_id,
                          "/".join(current.get(k, "?") for k in wanted),
                          "/".join(wanted.values())))
        if not args.fix:
            return match.group(0)
        rebuilt = "".join(f'\n            "{k}": {v},' for k, v in wanted.items())
        return head + rebuilt[:-1] + "\n          " + tail

    updated = BLOCK.sub(replace, text)

    print(f"{'model':34} {'ours (in/out/cR/cW)':>30} {'live':>30}")
    print("-" * 96)
    for model_id, ours, live in drift:
        print(f"{model_id:34} {ours:>30} {live:>30}")
    for model_id in missing:
        print(f"{model_id:34} {'':>30} {'NOT IN LIVE CATALOG':>30}")

    if not drift and not missing:
        print("(every shipped rate matches the live catalog)")

    if args.fix and drift:
        json.loads(re.sub(r"\{\{[^}]*\}\}", "PLACEHOLDER", updated))
        TEMPLATE.write_text(updated)
        print(f"\nrewrote {TEMPLATE.relative_to(Path.cwd())} — {len(drift)} rate(s) corrected")
        print("A mounted VM keeps the rates it was provisioned with; re-mount to pick these up.")
        return 0

    if drift:
        print(f"\n{len(drift)} rate(s) drifted. Re-run with --fix to correct the template.")
    if missing:
        print(f"{len(missing)} model(s) are not in OpenRouter's catalog — retired, or a typo'd id.")
    return 1 if (drift or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
