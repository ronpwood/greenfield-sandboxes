#!/usr/bin/env bash
# render_smoke_corpus.sh — run render_smoke.py over every harvested app, one row each.
#
# The calibration instrument for render_smoke assertions. A new signal is promoted
# from "reported" to "fails the build" only when this table shows it firing on the
# known-bad apps and on nothing else. That is the docstring's own bar: false
# positives are the failure mode to fear.
#
#     sandbox_mount/host/render_smoke_corpus.sh <repo> <out_dir> [ref ...]
#
#   repo     a checkout holding harvested runs under refs/sandbox/* (a target's checkout)
#   out_dir  scratch; each ref is extracted to <out_dir>/<id>/ and its report is <out_dir>/<id>.json
#   ref      default: every refs/sandbox/* in <repo>
#
# App dir comes from the ref's own app.manifest.yaml (`dir:`), falling back to apps/app.
# A ref with no index.html there is SKIPPED (plan-only or crashed runs), not failed.
# An existing <out_dir>/<id>/ is reused, so a hand-applied mutation fixture survives a re-run;
# delete it to re-extract.
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
smoke="${RENDER_SMOKE:-$here/../../adws/adw_modules/render_smoke.py}"   # override to calibrate a candidate copy
repo="${1:?usage: render_smoke_corpus.sh <repo> <out_dir> [ref ...]}"
out="${2:?usage: render_smoke_corpus.sh <repo> <out_dir> [ref ...]}"
shift 2
mkdir -p "$out"

if [ $# -gt 0 ]; then refs=("$@"); else
  refs=()
  while IFS= read -r r; do refs+=("$r"); done \
    < <(git -C "$repo" for-each-ref --format='%(refname)' refs/sandbox)
fi

printf '%-34s %-6s %5s %5s %6s %6s %7s %5s %5s %s\n' id passed ctrls unrch sector clkerr clicked G F note
for ref in "${refs[@]}"; do
  id="${ref##*/}"
  dest="$out/$id"
  if [ ! -d "$dest" ]; then
    mkdir -p "$dest"
    # A failed archive must not leave an empty dir behind: the reuse rule above would
    # then skip this ref forever as "no index.html".
    git -C "$repo" archive "$ref" | tar -x -C "$dest" \
      || { rm -rf "$dest"; printf '%-34s %-6s %s\n' "$id" ERR "archive failed: $ref"; continue; }
  fi
  appdir="$(sed -n 's/^[[:space:]]*dir:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$dest/app.manifest.yaml" 2>/dev/null | head -1)"
  appdir="${appdir:-apps/app}"
  if [ ! -f "$dest/$appdir/index.html" ]; then
    printf '%-34s %-6s %s\n' "$id" skip "no $appdir/index.html"; continue
  fi
  if [ ! -d "$dest/$appdir/node_modules" ] && [ -f "$dest/$appdir/package.json" ]; then
    (cd "$dest/$appdir" && bun install --frozen-lockfile >/dev/null 2>&1) \
      || (cd "$dest/$appdir" && bun install >/dev/null 2>&1)
  fi
  uv run "$smoke" "$dest/$appdir" --json > "$out/$id.json" 2> "$out/$id.err"
  code=$?
  if [ $code -eq 2 ] || ! jq -e . "$out/$id.json" >/dev/null 2>&1; then
    printf '%-34s %-6s %s\n' "$id" ERR "exit $code — see $out/$id.err"; continue
  fi
  jq -r --arg id "$id" '[$id, (.passed|tostring), (.controlCount//0), (.unreachable|length),
      ((.sectorFaults//[])|length), (.click_failures|length),
      "\(.clicked//"-")/\(.clickable//"-")",
      ([(.deadTextRings//[])[]|select(.fault)]|length),
      ([(.colourGroups//[])[]|select(.fault)]|length),
      ([(.deadTextRings//[])[]|select(.fault)|.group] + [(.colourGroups//[])[]|select(.fault)|"\(.group):\(.prop)"] | join(" "))] | @tsv' "$out/$id.json" \
    | awk -F'\t' '{printf "%-34s %-6s %5s %5s %6s %6s %7s %5s %5s %s\n",$1,$2,$3,$4,$5,$6,$7,$8,$9,$10}'
done
