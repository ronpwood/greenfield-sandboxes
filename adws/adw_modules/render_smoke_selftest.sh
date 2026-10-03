#!/usr/bin/env bash
# usage: bash adws/adw_modules/render_smoke_selftest.sh
# render_smoke's exit codes and click pass, on toy apps in a real Chromium
# (adws/adw_data/fixtures/render_smoke/). Each case was red before the fix
# (review 2026-10-02, CHANGELOG 2026-10-02e). Needs uv + playwright; no network.
HERE="$(cd "$(dirname "$0")" && pwd)"
R="$HERE/render_smoke.py"
F="$HERE/../adw_data/fixtures/render_smoke"
O="$(mktemp -d)"; trap 'rm -rf "$O"' EXIT
FAILS=0
case_() { if eval "$2"; then echo "PASS  $1"; else echo "FAIL  $1"; FAILS=$((FAILS+1)); fi; }

uv run "$R" "$F/noindex" > "$O/1" 2>&1; rc=$?
case_ "missing index.html exits 1 (blocking), not 2 (skipped)" '[ $rc = 1 ]'
uv run "$R" "$F/thirty" > "$O/2" 2>&1; rc=$?
case_ "buttons 26-30 that throw are clicked and FAIL" '[ $rc = 1 ] && grep -q "button \(2[6-9]\|30\) is broken" "$O/2"'
uv run "$R" "$F/fine" --max-clicks 5 > "$O/3" 2>&1; rc=$?
case_ "--max-clicks 5 (space form) honoured, reported PARTIAL" '[ $rc = 0 ] && grep -q "clicked 5 of 30" "$O/3" && grep -q "PARTIAL" "$O/3"'
uv run "$R" "$F/fine" > "$O/4" 2>&1; rc=$?
case_ "a clean 30-control app passes with all 30 clicked" '[ $rc = 0 ] && grep -q "clicked 30 of 30" "$O/4"'
uv run "$R" "$F/dead" > "$O/5" 2>&1; rc=$?
case_ "reachable controls with no click landed FAIL (floor)" '[ $rc = 1 ] && grep -q "not ONE click landed" "$O/5"'

if [ "$FAILS" = 0 ]; then echo "render_smoke selftest: all PASS"; exit 0; fi
for f in "$O"/*; do echo "--- case $(basename "$f")"; tail -4 "$f"; done
exit 1
