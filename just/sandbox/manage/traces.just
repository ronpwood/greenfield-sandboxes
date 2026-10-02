# TRACES. Pull a run's agent trace data back to the host.
#
# harvest brings home the COMMITS. This brings home the THINKING: the factory's
# adws/adw_data/ tree on the box — raw_output.jsonl streams (every thinking block
# and tool call the coding agent emitted), prompts, envelopes, agent_map.json, and
# the sssf.db SQLite mirror the observe UI polls. All of it is gitignored in the
# target repo, so none of it ever enters a bundle; without this recipe it dies
# with the VM at teardown. Teardown calls this before revoking the key; it is
# still runnable on its own, mid-run.
#
# Also pulls ~/.claude/projects when present — the Claude Code transcripts a
# `sbx run agent` arm leaves, which live outside the repo entirely.
#
# FAILS CLOSED. One probe ssh reports what exists and ends with a sentinel; a
# missing dir is normal (a bare `run agent` VM has no sessions/), but a probe
# that did not finish is an abort, never "nothing to pull".
#
# WHY rsync AND NOT scp. The session tree is a directory of small files and this
# recipe is meant to be re-run while the agent is still working; rsync moves only
# what changed. The db is copied with its -wal and -shm siblings because the
# tracer never checkpoints mid-run: the db file alone is a stale snapshot, the
# WAL holds the recent events.
#
# Lands under .sandbox/traces/<run-id>/ — beside the bundles, inside the same
# gitignored dir. IDEMPOTENT: re-running refreshes in place. Never touches the VM.

# pull a run's agent traces home: just sbx manage traces <run-id> [sssf-session-id]
traces RUN_ID SESSION="":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{ justfile_directory() }}"

    RR="sandbox_mount/host/run_record.py"
    RUN_ID="{{ RUN_ID }}"
    SESSION="{{ SESSION }}"
    DEST=".sandbox/traces/$RUN_ID"
    SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"

    "$RR" get "$RUN_ID" > /dev/null || { echo "!! traces: no run record for $RUN_ID"; exit 1; }
    VM=$("$RR" get "$RUN_ID" vm_name)
    [ -n "$VM" ] || { echo "   traces: no vm_name in the record — nothing to pull"; exit 0; }

    # SRC is the factory's data dir inside the clone. An optional SESSION narrows
    # the sessions/ pull to one sssf adw id; default is every session on the box.
    SRC="$VM.exe.xyz:app/adws/adw_data"
    mkdir -p "$DEST/sessions"

    PROBE=$(ssh $SSH_OPTS "$VM.exe.xyz" '[ -d app/adws/adw_data/sessions ] && echo sessions;
        ls app/adws/adw_data/sssf.db > /dev/null 2>&1 && echo db;
        [ -d .claude/projects ] && echo claude; echo SBX_PROBE_OK' < /dev/null) \
        || { echo "!! traces: could not reach $VM"; exit 1; }
    [ "$(printf '%s\n' "$PROBE" | tail -n1)" = SBX_PROBE_OK ] \
        || { echo "!! traces: probe of $VM did not complete"; exit 1; }
    has() { printf '%s\n' "$PROBE" | grep -qx "$1"; }

    if has sessions; then
        if [ -n "$SESSION" ]; then
            rsync -az --delete -e "ssh $SSH_OPTS" "$SRC/sessions/$SESSION/" "$DEST/sessions/$SESSION/" \
                || { echo "!! traces: could not pull sessions/$SESSION off $VM"; exit 1; }
        else
            rsync -az --delete -e "ssh $SSH_OPTS" "$SRC/sessions/" "$DEST/sessions/" \
                || { echo "!! traces: could not pull sessions/ off $VM"; exit 1; }
        fi
    fi
    if has db; then
        # The db and its WAL/SHM must travel together; --include keeps it to those three.
        rsync -az -e "ssh $SSH_OPTS" --include='sssf.db*' --exclude='*' "$SRC/" "$DEST/" \
            || { echo "!! traces: could not pull sssf.db off $VM"; exit 1; }
    fi
    if has claude; then
        # No --delete: the VM is the only source, and a re-pull must never shrink this.
        rsync -az -e "ssh $SSH_OPTS" "$VM.exe.xyz:.claude/projects/" "$DEST/claude-projects/" \
            || { echo "!! traces: could not pull ~/.claude/projects off $VM"; exit 1; }
    fi

    N=$(find "$DEST/sessions" -name raw_output.jsonl | wc -l | tr -d ' ')
    echo "   traces: $N agent stream(s) -> $DEST"
    echo "     sessions: $(ls "$DEST/sessions" | tr '\n' ' ')"
    has db && echo "     db:       $DEST/sssf.db (+wal/shm)"
    has claude && echo "     claude:   $DEST/claude-projects/ ($(find "$DEST/claude-projects" -name '*.jsonl' | wc -l | tr -d ' ') transcript(s))"
    echo "     thinking: jq -rj 'select(.type==\"message_update\" and .assistantMessageEvent.type==\"thinking_delta\") | .assistantMessageEvent.delta' $DEST/sessions/<id>/<agent>/raw_output.jsonl"
    echo "     query:    sqlite3 $DEST/sssf.db \"select seq,name,status from phases where adw_id='<id>' order by seq\""
