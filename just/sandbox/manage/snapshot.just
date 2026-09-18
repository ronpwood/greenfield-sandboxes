# SNAPSHOT. Commit a sandbox's uncommitted work onto its run branch so harvest
# can carry it home.
#
# The TDD chain leaves code UNCOMMITTED when review rejects it, on purpose — a
# red suite with no code is where a human picks TDD back up. A crashed, killed or
# timed-out chain leaves the same dirty tree. But harvest only carries commits,
# and teardown refuses to destroy a dirty tree, so without this the choices were
# a hand-typed `ssh … git commit` or losing the build. Across a four-arm fan-out
# where most arms may fail review, that is four chances to lose one.
#
# The logic lives in sandbox_mount/guest/snapshot_run_branch.sh and is PIPED OVER
# SSH rather than run from the VM's own checkout: the box then runs the host's
# copy, so a sandbox mounted from an older factory commit still gets today's
# behaviour, and the script can be tested against a throwaway local repo with no
# VM at all.
#
# It refuses unless HEAD is exactly sbx/<run-id>, so it can never commit to main
# or onto a detached HEAD, and the `UNAPPROVED snapshot:` subject plus a distinct
# author keep it from ever reading as approved work in `git log`.

# commit a sandbox's dirty tree onto sbx/<run-id>: just sbx manage snapshot <run-id> ["reason"]
snapshot RUN_ID *REASON:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{ justfile_directory() }}"

    RR="sandbox_mount/host/run_record.py"
    RUN_ID="{{ RUN_ID }}"
    REASON="{{ REASON }}"
    SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)

    "$RR" get "$RUN_ID" > /dev/null || { echo "!! snapshot: no run record for $RUN_ID" >&2; exit 1; }
    VM=$("$RR" get "$RUN_ID" vm_name)
    [ -n "$VM" ] || { echo "!! snapshot: no vm_name in the record for $RUN_ID — nothing to snapshot" >&2; exit 1; }

    echo "── snapshot $RUN_ID · vm $VM"

    # `|| true` so a non-zero exit is CLASSIFIED below instead of killing the
    # recipe under `set -e` — exit 2 (wrong branch) is a message, not a crash.
    # `ssh host cmd a b c` does NOT pass argv — it JOINS the arguments into one
    # string that the REMOTE shell re-splits on whitespace. A multi-word reason
    # therefore arrives as several positionals and the script sees only its first
    # word (observed 2026-09-18: "review_2 rejected: ..." landed as "review_2").
    # printf %q quotes it so the remote shell reassembles exactly one argument.
    # fill.just documents the same trap for its own clone arguments.
    Q_REASON=$(printf '%q' "$REASON")
    set +e
    OUT=$(ssh "${SSH_OPTS[@]}" "$VM".exe.xyz "bash -s -- app $(printf '%q' "$RUN_ID") $Q_REASON" \
            < sandbox_mount/guest/snapshot_run_branch.sh 2>&1)
    RC=$?
    set -e

    printf '%s\n' "$OUT" | sed 's/^/   /'

    case "$RC" in
        0)
            if printf '%s' "$OUT" | grep -q '^CLEAN'; then
                echo "   nothing to snapshot — the tree is clean (the chain committed its work)"
            else
                SHA=$(printf '%s' "$OUT" | awk '/^OK /{print $2}')
                echo "   committed ${SHA:0:12} on sbx/$RUN_ID"
                echo "   next: just sbx manage harvest $RUN_ID"
            fi
            ;;
        2)
            echo "!! snapshot: the VM's HEAD is not sbx/$RUN_ID, so nothing was committed." >&2
            echo "!! inspect it: just sbx run cmd $RUN_ID 'git status && git log --oneline -3'" >&2
            exit 2
            ;;
        *)
            echo "!! snapshot: failed on $VM (exit $RC) — the VM is untouched" >&2
            exit 1
            ;;
    esac
