#!/usr/bin/env bash
# provision.sh — turn a freshly cloned repo on a blank exeuntu VM into a
# running software factory. Runs INSIDE the sandbox:
#
#   ssh <vm> 'bash app/sandbox_mount/guest/provision.sh'
#
# Idempotent by construction: every install is skip-if-present and the tracer's
# DDL is CREATE TABLE IF NOT EXISTS. Re-running is cheap and safe.
#
# NEVER add apt to this file. Measured from the dal region: ~148 kB/s, ~35s per
# package. bun and just come from their own CDNs in ~1s combined.
set -euo pipefail

STEP="startup"
# shellcheck disable=SC2154   # rc is assigned by the trap body itself
trap 'rc=$?; echo "" >&2; echo "[provision] FAILED during: ${STEP} (line ${LINENO}, exit ${rc})" >&2; exit "$rc"' ERR

step() { STEP="$1"; echo ""; echo "── $1 ──────────────────────────────────"; }
say()  { echo "   $*"; }

# ── 1. locate the repo ───────────────────────────────────────────────────────
# Derived from this script's own path, never hardcoded to /home/exedev/app: the
# clone target is the host's choice and this file is the only thing that knows
# where it actually landed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"

step "1/9 repo root"
say "$REPO_ROOT"
say "commit $(git rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"

# ── 2. bun ───────────────────────────────────────────────────────────────────
# Versions come from toolchain.lock, never from a variable in this file. Each
# row is `<tool> <version> <mode>`, mode ∈ pin / float / image (defined in the
# lock's header). What actually landed is reported by toolchain_report.sh in
# step 9 and again by gate F in setup.just, so drift is visible, not frozen.
#
# HISTORY: bun 1.4.0 (2026-08-20) added a Host-header guard to `bun index.html`
# and the five 2026-08-21 mounts failed OBSERVE with a 403; the fix that day
# was a hard pin to 1.3.14. The serving layer no longer depends on it —
# observe.just fronts the dev server with sandbox_mount/guest/app_proxy.ts,
# which rewrites Host to localhost — so bun floats again.
# See specs/toolchain-unpin-and-drift-visibility.md and NEXTSTEPS.md (2026-08-30).
LOCK="${SCRIPT_DIR}/toolchain.lock"
[[ -f "$LOCK" ]] || { echo "[provision] missing ${LOCK}" >&2; exit 1; }
lock_want() { awk -v t="$1" '$1==t {print $2}' "$LOCK"; }
lock_mode() { awk -v t="$1" '$1==t {print $3}' "$LOCK"; }

want="$(lock_want bun)"; mode="$(lock_mode bun)"
# ONE-MOUNT ESCAPE HATCH: `BUN_VERSION=x.y.z just sbx mount <id>` — setup.just
# forwards it over ssh — overrides the lock and forces a pin at that version,
# for the day a fresh release is bad and there is no time for a lock commit.
if [[ -n "${BUN_VERSION:-}" ]]; then
  want="$BUN_VERSION"; mode="pin"
fi
step "2/9 bun (${mode} ${want})"
case "$mode" in
  pin)
    # Version-aware, not just presence-aware: `command -v bun` alone would
    # accept a WRONG version left on a re-provisioned or golden-copied VM and
    # skip the install silently — the class of bug a pin exists to kill.
    if [[ "$(bun --version 2>/dev/null || true)" == "$want" ]]; then
      say "already installed: $(bun --version)"
    else
      if command -v bun >/dev/null 2>&1; then
        say "found $(bun --version), replacing with pinned ${want}"
      fi
      # `-s bun-v<version>` is the installer's own pin argument.
      curl -fsSL https://bun.sh/install | bash -s "bun-v${want}"
      say "installed ${want}"
    fi
    ;;
  float)
    # Latest, and only when absent. NEVER upgrade or downgrade an existing
    # binary: a golden-copied VM keeps the bun it was built with, and gate F
    # makes that age visible instead of this script silently erasing it.
    if command -v bun >/dev/null 2>&1 || [[ -x "$HOME/.bun/bin/bun" ]]; then
      say "already installed — leaving it alone"
    else
      curl -fsSL https://bun.sh/install | bash
      say "installed latest"
    fi
    ;;
  *) echo "[provision] toolchain.lock: bun mode '${mode}' is not pin|float" >&2; exit 1 ;;
esac
# The installer only edits shell rc files, which this non-interactive shell never
# reads — put it on PATH by hand for the rest of the run.
if [[ -d "$HOME/.bun/bin" ]]; then
  export PATH="$HOME/.bun/bin:$PATH"
fi
# ...and symlink it where every FUTURE ssh session will find it. Each `ssh vm cmd`
# is a fresh non-interactive shell that reads no rc file, so a PATH export here
# dies with this script. OBSERVE hit exactly that: it started the app with nohup
# and got `bun: No such file or directory` while provision had just used bun
# successfully. just avoids this by installing to /usr/local/bin already.
if [[ -x "$HOME/.bun/bin/bun" ]] && [[ ! -e /usr/local/bin/bun ]]; then
  sudo ln -sf "$HOME/.bun/bin/bun" /usr/local/bin/bun
  sudo ln -sf "$HOME/.bun/bin/bunx" /usr/local/bin/bunx 2>/dev/null || true
  say "linked into /usr/local/bin for non-interactive ssh"
fi
command -v bun >/dev/null 2>&1 || { echo "[provision] bun not on PATH after install" >&2; exit 1; }
# A pin is asserted here, at the install, so a mismatch reads as an install
# problem and not as something three phases later. A float only records.
if [[ "$mode" == "pin" && "$(bun --version)" != "$want" ]]; then
  echo "[provision] bun version mismatch: wanted ${want}, got $(bun --version)" >&2
  exit 1
fi
say "bun $(bun --version) (${mode}, baseline ${want})"

# ── 3. just ──────────────────────────────────────────────────────────────────
# Same lock, same modes. Never apt. Note for anything that calls just in here
# later:  just --shell bash --shell-arg -c  — the root justfile sets `zsh -ic`
# and zsh is not in the image.
want="$(lock_want just)"; mode="$(lock_mode just)"
step "3/9 just (${mode} ${want})"
just_version() { just --version 2>/dev/null | awk '{print $2}'; }

# install.sh fetches the binary from github.com/casey/just/releases, and GitHub
# rate-limits release-asset downloads BY SOURCE IP. Every exe.dev VM leaves
# through the same egress, so a fan-out is N simultaneous unauthenticated
# fetches from one address: a six-way mount took a 403 on three arms, and a
# serialised retry 20s later still took two. bun is unaffected — bun.sh serves
# its own CDN. Retry with backoff rather than pinning a mirror: the block is
# transient (the same URL answered 302 minutes later) and a mirror is a second
# thing to trust. Bounded, so a genuinely bad tag still fails the provision.
just_install() {   # "$@" = extra args for install.sh
  local try delay=5
  for try in 1 2 3 4; do
    if curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh \
         | sudo bash -s -- --to /usr/local/bin "$@"; then
      return 0
    fi
    [[ $try -lt 4 ]] || break
    say "install failed (attempt ${try}/4) — GitHub rate-limits release assets per IP; retrying in ${delay}s"
    sleep "$delay"; delay=$(( delay * 3 ))
  done
  echo "[provision] just: install.sh failed 4 times (last delay ${delay}s)" >&2
  return 1
}

case "$mode" in
  pin)
    if [[ "$(just_version || true)" == "$want" ]]; then
      say "already installed: $(just --version)"
    else
      if command -v just >/dev/null 2>&1; then
        say "found $(just --version), replacing with pinned ${want}"
      fi
      # `--tag` is the installer's own version argument; `--force` lets it
      # overwrite the binary a previous provision left in /usr/local/bin.
      just_install --tag "$want" --force
      say "installed ${want}"
    fi
    ;;
  float)
    if command -v just >/dev/null 2>&1; then
      say "already installed — leaving it alone"
    else
      just_install
      say "installed latest"
    fi
    ;;
  *) echo "[provision] toolchain.lock: just mode '${mode}' is not pin|float" >&2; exit 1 ;;
esac
command -v just >/dev/null 2>&1 || { echo "[provision] just not on PATH after install" >&2; exit 1; }
if [[ "$mode" == "pin" && "$(just_version)" != "$want" ]]; then
  echo "[provision] just version mismatch: wanted ${want}, got $(just_version)" >&2
  exit 1
fi
say "just $(just_version) (${mode}, baseline ${want})"
unset want mode

# ── 4. pi model registry ─────────────────────────────────────────────────────
# ~/.pi/agent/models.json does not exist on a fresh VM, and without it
# `pi --list-models` prints "No models available" and EXITS 0 — the most likely
# silent mount failure there is. The cost block is all-or-nothing: a partial one
# fails schema validation and pi drops THE ENTIRE ROSTER.
step "4/9 pi models.json"
TMPL="sandbox_mount/guest/models.json.tmpl"
[[ -f "$TMPL" ]] || { echo "[provision] missing ${TMPL}" >&2; exit 1; }
mkdir -p "$HOME/.pi/agent"

models_json="$(cat "$TMPL")"
# The template ships apiKey "env:OPENROUTER_API_KEY". pi only sees that variable
# when its parent exported it — true for ADWs (uv run + dotenv), not true for a
# bare `ssh <vm> 'pi --list-models'`, which is exactly what the health gate runs.
# So bake the runtime key in when .env has one.
api_key=""
if [[ -f .env ]]; then
  api_key="$(grep -E '^[[:space:]]*(export[[:space:]]+)?OPENROUTER_API_KEY=' .env \
             | tail -n 1 | sed -E 's/^[^=]*=//; s/^["'"'"']//; s/["'"'"']$//' || true)"
fi
if [[ -n "$api_key" ]]; then
  models_json="${models_json//env:OPENROUTER_API_KEY/$api_key}"   # bash substitution, never argv
  say "runtime key baked in from .env"
else
  say "no OPENROUTER_API_KEY in .env — leaving the env: placeholder (pi will need it exported)"
fi
printf '%s\n' "$models_json" > "$HOME/.pi/agent/models.json"
chmod 600 "$HOME/.pi/agent/models.json"                            # it holds a live key
unset models_json api_key

# Fail HERE, at the moment the file is written, not only in `just sbx manage
# doctor` run by hand afterward. A partial cost block parses as valid JSON but
# fails pi's OWN schema validation, which then silently drops the entire
# roster (--list-models prints "No models available" and exits 0) — the
# comment above already names this as the most likely silent mount failure.
python3 -c '
import json, os, sys
required = {"input", "output", "cacheRead", "cacheWrite"}
path = os.path.expanduser("~/.pi/agent/models.json")
models = json.load(open(path))["providers"]["openrouter"]["models"]
bad = [m["id"] for m in models if set(m.get("cost", {})) != required]
if bad:
    sys.exit("models.json: %d model(s) with an incomplete cost block "
              "(pi silently drops the WHOLE roster for this): %s"
              % (len(bad), ", ".join(bad)))
' || { echo "[provision] models.json failed schema validation" >&2; exit 1; }
say "wrote $HOME/.pi/agent/models.json ($(grep -c '"id"' "$HOME/.pi/agent/models.json" || true) models)"

# ── 5. bun install ───────────────────────────────────────────────────────────
# apps/*/ instead of naming the app: `apps/` holds exactly one payload by
# convention (`just app swap` archives first), and a glob costs nothing here
# where a guest-side manifest read would cost a pyyaml fetch.
#
# --frozen-lockfile when a lock is COMMITTED, and it is load-bearing twice over.
# A plain `bun install` rewrites the lockfile whenever the guest's bun differs
# from whichever bun wrote it — the greenfield shell's lock was authored by the
# host's bun 1.3.0, and the guest's 1.4.2 adds a `"configVersion": 0` line. That
# is a one-line diff, and it dirtied the tree, and gate A (git integrity) failed
# every arm of a six-way fan-out before a single agent ran. Frozen also gets the
# property the committed lock exists for: every arm resolves the EXACT same
# dependency set, and a lock that genuinely no longer satisfies package.json
# fails here, loudly, instead of being silently rewritten into agreement.
step "5/9 bun install"
for dir in apps/*/ .claude/skills/sssf/apps/visualizer; do
  if [[ -f "$dir/package.json" ]]; then
    if [[ -f "$dir/bun.lock" || -f "$dir/bun.lockb" ]]; then
      ( cd "$dir" && bun install --frozen-lockfile )
      say "installed ${dir} (frozen)"
    else
      ( cd "$dir" && bun install )
      say "installed ${dir}"
    fi
  else
    say "skipped ${dir} (no package.json)"
  fi
done

# ── 6. build the visualizer UI ───────────────────────────────────────────────
# Without dist/ the server still boots but only answers the JSON API — the page
# itself 404s. `bunx vite build` rather than `bun run build`, which also runs
# vue-tsc; type errors must not be able to fail a mount.
step "6/9 visualizer build"
VIZ=".claude/skills/sssf/apps/visualizer"
if [[ -d "$VIZ" ]]; then
  ( cd "$VIZ" && bunx vite build )
  say "dist/ built"
else
  say "skipped (visualizer absent)"
fi

# ── 7. trace db ──────────────────────────────────────────────────────────────
# VERIFIED: the visualizer process EXITS when sssf.db is missing, and `just
# mount` ends at observe BEFORE any ADW has run — so a fresh sandbox would start
# a UI that dies instantly. Tracer's DDL is all CREATE TABLE IF NOT EXISTS, so
# calling it here is idempotent and stays correct if the schema moves.
step "7/9 trace db"
INIT_DB="$(mktemp -t sssf_init_db.XXXXXX.py)"
cat > "$INIT_DB" <<'PY'
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""Create sssf.db and its schema without running an ADW."""

import sys
from pathlib import Path

sys.path.insert(0, "adws")          # the import root `uv run adws/adw_*.py` gets

from adw_modules.agents import load_config
from adw_modules.tracer import Tracer

cfg = load_config()
db = Path(cfg.observability.db)
# Tracer only mkdirs the events file's PARENT, so passing the sessions dir
# itself creates that dir and leaves no stray session behind.
tracer = Tracer(db, Path(cfg.defaults.data_dir) / "sessions" / "events.jsonl")
tables = tracer.conn.execute(
    "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
print(f"   {db} — {tables} tables")
PY
uv run "$INIT_DB"
rm -f "$INIT_DB"

# ── 8. warm the uv cache ─────────────────────────────────────────────────────
# The PEP-723 resolve is ~3s cold and near zero after. Pay it here rather than
# inside the first agent run, where it looks like the agent hanging.
step "8/9 warm uv"
if uv run adws/adw_prompt.py --help >/dev/null 2>&1; then
  say "uv cache warm"
else
  echo "[provision] uv run adws/adw_prompt.py --help failed" >&2
  uv run adws/adw_prompt.py --help >&2 || true
  exit 1
fi

# ── 8c. headless chromium for the render gate ────────────────────────────────
# REVERSES a deliberate 2026-09-18 decision, and the reason it is reversed is
# evidence, not taste. That day's call was "chromium is ~190 MB and buys nothing
# on the VM that happy-dom does not already cover in the fix loop", so `just sbx
# manage shot` was built host-side instead. Fan-out 3 measured the gap in that
# reasoning: happy-dom does no LAYOUT and no HIT-TESTING, and six arms passed
# lint + typecheck + every test while two of them did not work. One had eleven of
# twelve key slices painted over by a single wrong SVG arc flag. `shot` runs
# after the chain is over and gates nothing, so the finding arrived too late to
# repair. A gate has to live where a failure can still be fixed.
#
# Cost is real and accepted: a chromium download per mount. Best-effort ON
# PURPOSE -- `render` treats "could not open a browser" (exit 2) as a skip, not a
# failure, so a slow or blocked CDN degrades the chain to exactly what it was
# yesterday instead of failing every arm of a fan-out. Retried for the same
# reason the `just` installer is: a shared egress IP makes N concurrent arms N
# simultaneous unauthenticated downloads.
step "8c/9 headless chromium (render gate)"
if uv run --with playwright python -c 'import playwright' >/dev/null 2>&1; then
  installed=""
  for try in 1 2 3; do
    if uv run --with playwright playwright install --with-deps chromium >/dev/null 2>&1 \
       || uv run --with playwright playwright install chromium >/dev/null 2>&1; then
      installed=yes; break
    fi
    say "chromium install failed (attempt ${try}/3), retrying in $(( try * 10 ))s"
    sleep $(( try * 10 ))
  done
  if [[ -n "$installed" ]]; then
    say "chromium ready — the render gate will run"
  else
    say "chromium NOT installed — the render gate will SKIP (exit 2), chain still green"
  fi
else
  say "playwright unavailable — the render gate will SKIP (exit 2), chain still green"
fi

# ── 8b. pre-answer Claude Code's interactive onboarding ──────────────────────
# `claude -p` (the `just sbx run agent` lane) skips onboarding, so nothing here
# exercised it until someone attached INTERACTIVELY with `claude --resume`.
# Interactive first run blocks on three gates in a row: theme picker, then
# "Detected a custom API key in your environment — use it?", then login method.
#
# The middle gate is the load-bearing one and its default is NO. Answering no is
# unrecoverable on a headless VM: ANTHROPIC_API_KEY=implicit plus
# ANTHROPIC_BASE_URL=llm.int.exe.xyz IS the key-free exe.dev gateway, so
# declining it falls through to a login prompt that can never complete here.
# The approval is keyed by the literal token "implicit" — the same string the
# env var carries.
#
# Idempotent: it rewrites three keys and preserves everything else in the file.
step "8b/9 claude onboarding"
if command -v claude >/dev/null 2>&1; then
  python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude.json")
d = json.load(open(p)) if os.path.exists(p) else {}
d["hasCompletedOnboarding"] = True
d["theme"] = "dark"
r = d.setdefault("customApiKeyResponses", {})
r["approved"] = sorted(set(r.get("approved", [])) | {"implicit"})
r["rejected"] = [x for x in r.get("rejected", []) if x != "implicit"]
json.dump(d, open(p, "w"))
PY
  say "onboarding pre-answered (theme, implicit key approved)"
else
  say "claude not installed — skipping"
fi

# ── summary ──────────────────────────────────────────────────────────────────
step "9/9 summary"
say "repo    $REPO_ROOT @ $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
# One source of truth for "what is on this box": the same script gate F runs.
# Informational here (a pin mismatch on bun/just already failed above, and the
# gate is the enforcer for the rest), hence the `|| true`.
bash "${SCRIPT_DIR}/toolchain_report.sh" | sed 's/^/   /' || true
# `|| true` inside the pipeline, not after it: pipefail would otherwise hand the
# failure of an absent/unhappy pi to the ERR trap and skip the sentinel below.
say "models  $( { pi --list-models 2>/dev/null || true; } | grep -c . || true ) lines from pi --list-models"
echo ""
echo "[provision] READY"

# The caller polls for this file — there is no other reliable completion signal.
# It must stay the last line: anything after it can fail after the host has
# already been told the sandbox is ready.
touch /tmp/PROVISION_READY
