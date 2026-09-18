#!/usr/bin/env bash
# snapshot_run_branch.sh <repo-dir> <run-id> [reason]
#
# Commit a sandbox's UNCOMMITTED work onto its own run branch, so it can be
# harvested instead of thrown away.
#
# WHY THIS EXISTS. adw_tdd_sdlc.py deliberately leaves code uncommitted when
# review rejects it — "a red suite with no code is exactly where a human picks
# TDD back up" — and a chain that crashes, times out or is killed leaves the
# same dirty tree. But harvest only carries COMMITS, and teardown refuses a
# dirty tree. On 2026-09-17 a rejected build was rescued by a hand-typed
# `ssh … git commit`. Across a four-arm fan-out where most arms may fail review,
# that manual step is four chances to lose a build.
#
# WHY IT IS NOT A CHANGE TO THE CHAIN. Committing rejected code inside the chain
# would make "the latest commit is approved" untrue, and changing commit
# semantics in the TDD chain but not the simple one would break the A/B those
# two chains exist to provide. This is a separate, explicit act by the operator,
# and the `UNAPPROVED snapshot:` subject plus a distinct author make it
# impossible to mistake for an approved build in `git log`.
#
# WHY IT LIVES HERE AND IS PIPED OVER SSH. `ssh vm bash -s -- args < this-file`
# means the VM runs the HOST's copy, so a sandbox mounted from an older factory
# commit still gets today's behaviour, and this file can be tested locally
# against a throwaway repo with no VM at all (see just/sandbox/manage/snapshot.just).
#
# Exit codes: 0 committed or already clean · 2 wrong branch · 1 anything else.

set -euo pipefail

REPO_DIR="${1:?usage: snapshot_run_branch.sh <repo-dir> <run-id> [reason]}"
RUN_ID="${2:?usage: snapshot_run_branch.sh <repo-dir> <run-id> [reason]}"
REASON="${3:-}"
[ -n "$REASON" ] || REASON="uncommitted work at snapshot time"

cd "$REPO_DIR" || { echo "snapshot: no such directory: $REPO_DIR" >&2; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "snapshot: $REPO_DIR is not a git repo" >&2; exit 1; }

# The branch check is the whole safety story, so it is first and it is strict.
# `symbolic-ref` (not `rev-parse --abbrev-ref`) because a DETACHED HEAD must be
# a refusal, not the string "HEAD" that a looser check would happily compare.
# Committing a half-finished agent run onto main, or onto a detached HEAD where
# it would be unreachable, are both worse than losing it.
WANT="sbx/$RUN_ID"
HAVE="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
if [ "$HAVE" != "$WANT" ]; then
    echo "snapshot: refusing — HEAD is '${HAVE:-<detached>}', expected '$WANT'" >&2
    echo "snapshot: this only ever commits to a run's own branch" >&2
    exit 2
fi

# Nothing to do is a SUCCESS, not an error: the caller is a fan-out loop that
# snapshots every arm without knowing which ones the chain left dirty.
if [ -z "$(git status --porcelain)" ]; then
    echo "CLEAN"
    exit 0
fi

# `git add -A` honours .gitignore, which is what keeps app/.env — the runtime
# OpenRouter key — out of the commit and therefore out of the harvested bundle.
# That is a real secret in a real file on every sandbox, so it is asserted
# rather than assumed: if .env is not ignored here, stop.
git add -A
if git diff --cached --name-only | grep -qx '.env'; then
    echo "snapshot: refusing — .env is STAGED, so it is not gitignored in this repo." >&2
    echo "snapshot: that file holds the runtime API key. Fix .gitignore first." >&2
    git reset -q
    exit 1
fi

STAT="$(git diff --cached --stat | tail -n 40)"
FILES="$(git diff --cached --name-only | wc -l | tr -d ' ')"

# The newest adw session id, if the factory left one — it is the key into the
# traces this snapshot belongs to, and reading it back off a bundle later is
# much easier than reconstructing it from timestamps.
ADW_ID=""
if [ -d adws/adw_data/sessions ]; then
    ADW_ID="$(ls -1t adws/adw_data/sessions 2>/dev/null | head -n 1 || true)"
fi

# A distinct author, so `git log --author` separates snapshots from agent work.
# -c rather than exported env: it applies to this one commit and leaves no
# lingering config on the box.
git \
  -c user.name='sssf-snapshot' \
  -c user.email='sssf-snapshot@localhost' \
  commit -q --no-verify -m "UNAPPROVED snapshot: ${REASON}" -m "\
This commit was NOT produced or approved by the review phase. It is the
sandbox's working tree at snapshot time, committed so it could be harvested
instead of destroyed at teardown.

run:    ${RUN_ID}
branch: ${WANT}
adw:    ${ADW_ID:-<none recorded>}

${STAT}"

echo "OK $(git rev-parse HEAD) ${FILES}"
