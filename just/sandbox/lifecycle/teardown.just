# Phase 6 — TEARDOWN. Host only, and only when we decide.
#
# IMPORTED into the root justfile (not a module): no `set` lines here, and the
# root's `set dotenv-load` is what puts OPENROUTER_PROVISIONING_KEY in scope.
# No file-level variables either — six imports share one scope, so a duplicate
# name across phase files is a hard error. Everything is a bash local instead.
#
# Order is load-bearing: spend captured -> artifacts out -> commits harvested ->
# dirty tree checked -> key revoked -> VM destroyed. A crash between revoke and
# destroy leaves a dead key and a live VM (visible, cheap). The reverse leaves a
# live key nobody can find (invisible, spends). This is the ONLY file allowed to
# destroy a VM.
#
# Everything that READS the box runs before anything that destroys it, and both
# the harvest failing AND the working tree being dirty ABORT the whole teardown:
# destroying a VM whose commits were never pulled, or whose uncommitted work was
# never even committed, are the two irreversible mistakes available in this file.
#
# IDEMPOTENT by construction: every step is individually guarded, because a run
# can die anywhere — VM but no key, key but no VM, a record with neither. A
# missing piece is the normal path, not an error.

# tear down one run: spend -> artifacts -> harvest -> dirty-check -> revoke -> destroy -> close -> gate
teardown RUN_ID *FLAGS:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{ justfile_directory() }}"

    RR="sandbox_mount/host/run_record.py"
    RUN_ID="{{ RUN_ID }}"
    RUNS=".sandbox/runs"
    KEY_FILE="$RUNS/$RUN_ID.key"
    ART="$RUNS/$RUN_ID-artifacts"
    API="https://openrouter.ai/api/v1"

    # Through a variable, not straight into `for ... in`: interpolating an empty
    # variadic into the list is a syntax error, via a variable it is zero iterations.
    RAW_FLAGS="{{ FLAGS }}"
    HARVEST=1
    FORCE_DIRTY=0
    for a in $RAW_FLAGS; do
        case "$a" in
            --no-harvest) HARVEST=0 ;;
            --force-dirty) FORCE_DIRTY=1 ;;
            *) echo "teardown: unknown flag $a (only --no-harvest, --force-dirty)"; exit 2 ;;
        esac
    done
    # All *.exe.xyz share one host key and a wildcard known_hosts entry is already
    # installed, so no StrictHostKeyChecking games. BatchMode so a teardown never
    # sits at a password prompt; timeouts so it never hangs on a dead VM.
    SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)

    echo "── teardown $RUN_ID"

    # The record is the only handle on the key. Without it there is nothing to
    # revoke and nothing to close, so this is the one hard prerequisite.
    "$RR" get "$RUN_ID" > /dev/null || { echo "!! no run record for $RUN_ID — nothing to tear down"; exit 1; }
    VM=$("$RR" get "$RUN_ID" vm_name)
    KEY_HASH=$("$RR" get "$RUN_ID" key_hash)
    CLOSED=$("$RR" get "$RUN_ID" closed_at)
    [ -n "$CLOSED" ] && echo "   (record already closed at $CLOSED — re-running the remaining steps)"

    # ── does the VM still exist? -------------------------------------------------
    # Ask the control plane rather than probing ssh: authoritative, and the same
    # answer `reap` uses. Verified shape: {"vms":[{"vm_name":"...",...}]}
    VM_ALIVE=0
    if [ -n "$VM" ]; then
        VM_LIST=$(ssh "${SSH_OPTS[@]}" exe.dev ls --json 2>/dev/null || echo '{"vms":[]}')
        if printf '%s' "$VM_LIST" | VM="$VM" python3 -c \
            'import json,os,sys; sys.exit(0 if os.environ["VM"] in {v.get("vm_name") for v in json.load(sys.stdin).get("vms",[])} else 1)'
        then
            VM_ALIVE=1
        fi
    fi
    if [ "$VM_ALIVE" = 1 ]; then echo "   vm:  $VM (running)"; else echo "   vm:  ${VM:-<none>} (gone or never created)"; fi

    # ── 1. spend, BEFORE the key dies -------------------------------------------
    # This is what makes best-of-N comparable: same prompt, N models, cost beside
    # result. Read it now — after DELETE the number is unrecoverable.
    # NOTE: the runtime key is a SANDBOX secret; the provisioning key never leaves
    # this host and is never used for the per-key usage read.
    SPEND=""
    if [ -s "$KEY_FILE" ]; then
        RUNTIME_KEY=$(cat "$KEY_FILE")
        # GET /api/v1/key reports the calling key's own usage. Verified shape:
        # {"data":{"usage":0.0081,"limit":50.0,...}}
        SPEND=$(curl -sS --max-time 30 "$API/key" -H "Authorization: Bearer $RUNTIME_KEY" 2>/dev/null \
                | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["usage"])' 2>/dev/null || true)
        unset RUNTIME_KEY
    elif [ -n "$KEY_HASH" ] && [ -n "${OPENROUTER_PROVISIONING_KEY:-}" ]; then
        # Re-run after the .key file was already shredded: the provisioning LIST
        # still carries `usage` per hash, so spend is recoverable from there.
        SPEND=$(curl -sS --max-time 30 "$API/keys" -H "Authorization: Bearer $OPENROUTER_PROVISIONING_KEY" 2>/dev/null \
                | KH="$KEY_HASH" python3 -c \
                  'import json,os,sys; print(next((k["usage"] for k in json.load(sys.stdin).get("data",[]) if k.get("hash")==os.environ["KH"]), ""))' 2>/dev/null || true)
    fi
    if [ -n "$SPEND" ]; then
        "$RR" set "$RUN_ID" spend="$SPEND"
        echo "   spend: \$$SPEND recorded"
    elif [ -z "$KEY_HASH" ]; then
        echo "   spend: n/a — no key was ever minted for this run"
    else
        echo "   spend: unavailable (key file gone, hash no longer listed) — leaving record as-is"
    fi

    # ── 2. artifacts ------------------------------------------------------------
    if [ "$VM_ALIVE" = 1 ]; then
        mkdir -p "$ART"
        TAR_TMP=$(mktemp -t "sbx-$RUN_ID")
        # tracer.py runs sssf.db in WAL mode (see its header) so the UI can read
        # while ADWs write: recent events sit in adws/adw_data/sssf.db-wal until
        # checkpointed, and a plain file copy of sssf.db alone can miss all of
        # them — this is what actually produced the "zero events for this
        # adw_id" backup the first sandbox session hit, not a wrong path. A
        # checkpoint merges the WAL into the main file so the single-file copy
        # below is complete. Best-effort: a failed/skipped checkpoint still
        # copies whatever the main file has, same as before this fix existed.
        ssh "${SSH_OPTS[@]}" "$VM".exe.xyz \
            'cd app 2>/dev/null && python3 -c "import sqlite3; c = sqlite3.connect(\"adws/adw_data/sssf.db\"); c.execute(\"PRAGMA busy_timeout=5000;\"); c.execute(\"PRAGMA wal_checkpoint(FULL);\")" 2>&1' \
            | sed 's/^/   trace db checkpoint: /' || true
        # One round trip, and `ls -d` filters to what actually exists so a run that
        # died before writing app_docs/ is not an error.
        ssh "${SSH_OPTS[@]}" "$VM".exe.xyz \
            'cd app 2>/dev/null || exit 0; P=$(ls -d specs app_docs adws/adw_data/sssf.db run.log 2>/dev/null); [ -z "$P" ] && exit 0; tar cf - $P' \
            > "$TAR_TMP" || true
        if [ -s "$TAR_TMP" ]; then
            tar xf "$TAR_TMP" -C "$ART"
            echo "   artifacts -> $ART"
        else
            echo "   artifacts: none found on the vm"
            rmdir "$ART" 2> /dev/null || true   # do not leave an empty dir implying we pulled something
        fi
        rm -f "$TAR_TMP"
    else
        echo "   artifacts: skipped (no vm)"
    fi

    # ── 3. harvest the commits --------------------------------------------------
    # File copies LOSE history, and SSSF commits plan, code and docs as separate
    # commits — the shape of the run is in the commit graph, not the files. The
    # bundling lives in harvest.just so it can also be run on its own, at any time,
    # without committing to a teardown. Default ON here so nobody loses a run's work
    # by forgetting; a failure STOPS before revoke and destroy.
    if [ "$HARVEST" != 1 ]; then
        echo "   harvest: skipped (--no-harvest)"
    elif [ "$VM_ALIVE" = 1 ]; then
        just sbx manage harvest "$RUN_ID" || {
            echo "!! harvest FAILED — stopping before revoke and destroy. The VM is left alive."
            echo "!! diagnose, then re-run:      just sbx lifecycle teardown $RUN_ID"
            echo "!! or skip it deliberately:    just sbx lifecycle teardown $RUN_ID --no-harvest"
            exit 1
        }
    else
        echo "   harvest: skipped (no vm)"
    fi

    # ── 4. dirty working tree check ---------------------------------------------
    # harvest only pulls committed history via a bundle — uncommitted work on the
    # VM is invisible to it. This is the one irreversible mistake NEXTSTEPS.md
    # found the hard way: a run's real changes existed only in the VM's working
    # tree, the gate passed anyway (see the fix in adws/adw_modules/gates.py),
    # and destroying the VM erased them for good. Refuse rather than auto-stash —
    # a stash nobody pops is exactly as lost as never harvesting, and this
    # codebase's gates never guess, they check.
    if [ "$VM_ALIVE" = 1 ] && [ "$FORCE_DIRTY" != 1 ]; then
        DIRTY=$(ssh "${SSH_OPTS[@]}" "$VM".exe.xyz 'cd app 2>/dev/null && git status --porcelain 2>/dev/null' || true)
        if [ -n "$DIRTY" ]; then
            echo "!! $VM's working tree is dirty — this uncommitted work would be destroyed:"
            echo "$DIRTY" | sed 's/^/!!   /'
            echo "!! refusing to destroy. VM left alive. Either:"
            echo "!!   inspect and commit/discard by hand over ssh, then re-run teardown"
            echo "!!   or accept the loss:  just sbx lifecycle teardown $RUN_ID --force-dirty"
            exit 1
        fi
        echo "   tree: clean"
    elif [ "$FORCE_DIRTY" = 1 ]; then
        echo "   tree: dirty-tree check skipped (--force-dirty)"
    fi

    # ── 5. revoke the key -------------------------------------------------------
    if [ -z "$KEY_HASH" ]; then
        echo "   key: none minted for this run — nothing to revoke"
    elif [ -z "${OPENROUTER_PROVISIONING_KEY:-}" ]; then
        echo "!! key_hash $KEY_HASH is live but OPENROUTER_PROVISIONING_KEY is unset — cannot revoke."
        echo "!! set it in .env and re-run: a live key keeps spending. VM left alone."
        exit 1
    else
        CODE=$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' -X DELETE "$API/keys/$KEY_HASH" \
               -H "Authorization: Bearer $OPENROUTER_PROVISIONING_KEY")
        case "$CODE" in
            2*)  echo "   key: revoked ($KEY_HASH)" ;;
            404) echo "   key: already gone ($KEY_HASH)" ;;   # idempotent re-run
            *)   echo "!! DELETE $API/keys/$KEY_HASH returned $CODE — key may still be live. VM left alone."; exit 1 ;;
        esac
    fi

    # ── 6. destroy the vm -------------------------------------------------------
    if [ "$VM_ALIVE" = 1 ]; then
        ssh "${SSH_OPTS[@]}" exe.dev rm "$VM" > /dev/null
        echo "   vm: destroyed ($VM)"
    else
        echo "   vm: nothing to destroy"
    fi

    # ── 7. close the record, shred the key file ---------------------------------
    "$RR" close "$RUN_ID"
    if [ -e "$KEY_FILE" ]; then
        # shred where available, `rm -P` on macOS; -f last so a re-run never fails
        # on an already-removed file.
        if command -v shred > /dev/null 2>&1; then shred -u "$KEY_FILE" 2>/dev/null || rm -f "$KEY_FILE"
        else rm -Pf "$KEY_FILE" 2>/dev/null || rm -f "$KEY_FILE"; fi
        echo "   key file shredded"
    fi
    echo "   record closed"

    # ── 8. GATE: the key is ABSENT from the key LIST ----------------------------
    # MEASURED: right after a successful DELETE, GET /api/v1/key still returns 200
    # for the dead key. The LIST is the authoritative view. Never gate on /key.
    if [ -z "$KEY_HASH" ]; then
        echo "✓ teardown complete for $RUN_ID (no key was ever minted)"
        exit 0
    fi
    LIST=$(curl -sS --max-time 30 "$API/keys" -H "Authorization: Bearer $OPENROUTER_PROVISIONING_KEY") \
        || { echo "!! gate: could not read $API/keys — revocation UNVERIFIED"; exit 1; }
    if printf '%s' "$LIST" | KH="$KEY_HASH" python3 -c \
        'import json,os,sys; sys.exit(1 if os.environ["KH"] in {k.get("hash") for k in json.load(sys.stdin).get("data",[])} else 0)'
    then
        echo "✓ gate: key absent from $API/keys"
        echo "✓ teardown complete for $RUN_ID"
    else
        echo "!! gate FAILED: $KEY_HASH is still listed at $API/keys. The key is LIVE."
        echo "!! revoke it by hand: curl -X DELETE $API/keys/$KEY_HASH -H 'Authorization: Bearer \$OPENROUTER_PROVISIONING_KEY'"
        exit 1
    fi
