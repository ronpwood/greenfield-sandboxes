#!/usr/bin/env bash
# toolchain_report.sh — what is actually on this box, against toolchain.lock.
#
#   bash toolchain_report.sh [--json] [<lock-file>]
#
# Table mode prints one row per lock entry:  tool  baseline  actual  status
#   ok       actual == baseline
#   DRIFT    actual != baseline (a float or image row moved — informational)
#   MISSING  the tool is not on PATH
#   record   baseline is `unknown` — record-only until someone seeds the row
# --json prints {"bun":"1.4.0","just":"1.58.0",...} (null when missing) for the
# run record and nothing else.
#
# Exit 1 ONLY when a `pin` row's actual is not its baseline (DRIFT or MISSING).
# float/image drift never fails: making it visible is the whole point, and the
# bump ritual (see the lock header) is how a DRIFT becomes the new baseline.
#
# Shared by provision.sh (step 9 summary) and setup.just (gate F), and it
# inspects binaries rather than install logs, so a golden-copied VM that never
# ran an install step is reported just as honestly as a fresh one.
set -uo pipefail

JSON=0
LOCK=""
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    *) LOCK="$a" ;;
  esac
done
[[ -n "$LOCK" ]] || LOCK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/toolchain.lock"
[[ -f "$LOCK" ]] || { echo "toolchain_report: no lock file at $LOCK" >&2; exit 2; }

# One resolver per tool. Output is the bare version or empty when absent.
# Each is a single line so a new tool is a one-line addition here plus a row
# in the lock.
actual_of() {
  case "$1" in
    bun)    bun --version 2>/dev/null ;;
    just)   just --version 2>/dev/null | awk '{print $2}' ;;
    uv)     uv --version 2>/dev/null | awk '{print $2}' ;;
    pi)     pi --version 2>/dev/null | awk '{print $NF}' ;;
    claude) claude --version 2>/dev/null | awk '{print $1}' ;;
    python) python3 --version 2>/dev/null | awk '{print $2}' ;;
    *)      "$1" --version 2>/dev/null | head -n 1 ;;
  esac | head -n 1 | tr -cd 'A-Za-z0-9._+-'
}

rc=0
json=""
[[ "$JSON" -eq 1 ]] || printf '%-8s %-10s %-10s %s\n' tool baseline actual status
while read -r tool want mode _; do
  [[ -z "$tool" || "$tool" == \#* ]] && continue
  actual="$(actual_of "$tool")"
  if [[ -z "$actual" ]];          then status=MISSING
  elif [[ "$want" == unknown ]];  then status=record
  elif [[ "$actual" == "$want" ]]; then status=ok
  else                                 status=DRIFT
  fi
  if [[ "$mode" == pin && "$status" != ok ]]; then rc=1; fi
  if [[ "$JSON" -eq 1 ]]; then
    if [[ -n "$actual" ]]; then v="\"$actual\""; else v=null; fi
    json="${json:+$json,}\"$tool\":$v"
  else
    printf '%-8s %-10s %-10s %s%s\n' "$tool" "$want" "${actual:-—}" "$status" \
      "$([[ "$status" == DRIFT && "$mode" != pin ]] && echo "  ($mode)")"
  fi
done < "$LOCK"

if [[ "$JSON" -eq 1 ]]; then
  printf '{%s}\n' "$json"
elif [[ "$rc" -ne 0 ]]; then
  echo "toolchain_report: a pinned tool does not match its baseline" >&2
fi
exit "$rc"
