#!/usr/bin/env bash
# usage: bash sandbox_mount/host/teardown_selftest.sh
# Drives teardown and reap down every failure path with PATH shims for ssh, curl
# and rsync, and asserts each one FAILS CLOSED: a probe we could not answer
# aborts before any key DELETE, and the record stays open. The `happy` case is
# the guard against an over-permissive shim — if the shims were wrong, the happy
# path would break too. Exit 0 only when every case behaves. No VM, no network,
# no spend: curl is a shim, and the script refuses to run if it is not.
set -euo pipefail
cd "$(dirname "$0")/../.."
RR="sandbox_mount/host/run_record.py"
T="$(mktemp -d)"
mkdir -p "$T/bin"
IDS=()
cleanup() {
    for id in ${IDS[@]+"${IDS[@]}"}; do
        rm -f "$("$RR" path "$id" 2>/dev/null)" ".sandbox/runs/$id.key"
        rm -rf ".sandbox/runs/$id-artifacts" ".sandbox/traces/$id"
    done
    rm -rf "$T"
}
trap cleanup EXIT

# ── shims: log argv (Bearer tokens redacted), answer per $FAKE_MODE ─────────────
cat > "$T/bin/ssh" <<'SH'
#!/usr/bin/env bash
echo "ssh $*" | sed 's/Bearer [^ ]*/Bearer ***/g' >> "$FAKE_LOG"
case "$*" in
  *"exe.dev ls"*)
    case "$FAKE_MODE" in
      ls-fail) exit 255 ;;
      ls-garbage) echo "not json" ;;
      *) echo "{\"vms\":[{\"vm_name\":\"$FAKE_VM\"}]}" ;;
    esac ;;
  *"exe.dev rm"*) exit 0 ;;
  *SBX_PROBE_OK*)
    [ "$FAKE_MODE" = traces-fail ] && exit 255
    echo SBX_PROBE_OK ;;
  *SBX_TREE_OK*)
    case "$FAKE_MODE" in
      dirty-fail) exit 255 ;;
      dirty-real) printf ' M apps/x.ts\nSBX_TREE_OK\n' ;;
      *) echo SBX_TREE_OK ;;
    esac ;;
  *) exit 0 ;;
esac
SH
cat > "$T/bin/curl" <<'SH'
#!/usr/bin/env bash
echo "curl $*" | sed 's/Bearer [^ ]*/Bearer ***/g' >> "$FAKE_LOG"
case "$*" in
  *"-X DELETE"*) echo 204 ;;
  *"/keys "*|*/keys) if [ -n "${FAKE_KEYS:-}" ]; then cat "$FAKE_KEYS"; else echo '{"data":[]}'; fi ;;
  *"/key "*|*/key) echo '{"data":{"usage":0.01}}' ;;
  *) exit 7 ;;
esac
SH
cat > "$T/bin/rsync" <<'SH'
#!/usr/bin/env bash
echo "rsync $*" >> "$FAKE_LOG"
for last; do :; done
mkdir -p "$last"
SH
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" OPENROUTER_PROVISIONING_KEY=selftest-dummy
[ "$(command -v curl)" = "$T/bin/curl" ] || { echo "!! curl shim not first on PATH — refusing to run"; exit 1; }

FAILS=0
record() {   # new throwaway open record with a VM and a fake key -> $id
    # Sets a global rather than echoing: a $(record) subshell would lose IDS+=.
    id="selftest-td-$RANDOM$RANDOM"
    IDS+=("$id")
    "$RR" create "$id" > /dev/null
    "$RR" set "$id" vm_name="$id" key_hash="deadbeef$RANDOM" > /dev/null
    echo dummy > ".sandbox/runs/$id.key"
}
check() {    # name want_exit(0|nz) want_delete(yes|no) want_closed(yes|no|-) got_exit id out phrase
    # The phrase pins WHY it stopped: an abort for some unrelated error would
    # otherwise pass a fail-closed case.
    local name="$1" we="$2" wd="$3" wc="$4" ge="$5" id="$6" out="$7" phrase="$8" ok=1 gd=no gc=no
    grep -q -- "-X DELETE" "$FAKE_LOG" && gd=yes
    [ -n "$("$RR" get "$id" closed_at 2>/dev/null)" ] && gc=yes
    if [ "$we" = 0 ]; then [ "$ge" = 0 ] || ok=0; else [ "$ge" != 0 ] || ok=0; fi
    [ "$gd" = "$wd" ] || ok=0
    [ "$wc" = - ] || [ "$gc" = "$wc" ] || ok=0
    grep -qF -- "$phrase" "$out" || ok=0
    if [ "$ok" = 1 ]; then echo "PASS  $name"
    else echo "FAIL  $name  (exit=$ge delete=$gd closed=$gc; want exit=$we delete=$wd closed=$wc, output containing \"$phrase\")"; FAILS=$((FAILS+1)); fi
}

phrase() {
    case "$1" in
        ls-fail)     echo "could not list VMs" ;;
        ls-garbage)  echo "does not parse" ;;
        traces-fail) echo "traces FAILED" ;;
        dirty-fail)  echo "unknown is not clean" ;;
        dirty-real)  echo "working tree is dirty" ;;
        happy)       echo "teardown complete" ;;
    esac
}
for mode in ls-fail ls-garbage traces-fail dirty-fail dirty-real happy; do
    record
    export FAKE_MODE="$mode" FAKE_VM="$id" FAKE_LOG="$T/$mode.log"; : > "$FAKE_LOG"
    rc=0; just sbx lifecycle teardown "$id" --no-harvest > "$T/$mode.out" 2>&1 || rc=$?
    if [ "$mode" = happy ]; then check "teardown $mode" 0 yes yes "$rc" "$id" "$T/$mode.out" "$(phrase $mode)"
    else check "teardown $mode" nz no no "$rc" "$id" "$T/$mode.out" "$(phrase $mode)"; fi
done

# reap: an open run's key, and the VM listing fails. Must refuse, never revoke.
record
KH=$("$RR" get "$id" key_hash)
printf '{"data":[{"name":"sbx-%s","hash":"%s","usage":0.01}]}' "$id" "$KH" > "$T/keys.json"
export FAKE_MODE=ls-fail FAKE_VM="$id" FAKE_LOG="$T/reap.log" FAKE_KEYS="$T/keys.json"; : > "$FAKE_LOG"
rc=0; just sbx manage reap --yes > "$T/reap.out" 2>&1 || rc=$?
check "reap --yes ls-fail" nz no - "$rc" "$id" "$T/reap.out" "could not list VMs"

if [ "$FAILS" != 0 ]; then
    echo "teardown selftest: $FAILS case(s) FAILED — outputs:"
    for f in "$T"/*.out; do echo "--- $(basename "$f")"; tail -n 6 "$f"; done
    exit 1
fi
echo "teardown selftest: all cases PASS"
