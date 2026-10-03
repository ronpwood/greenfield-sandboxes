# just/sandbox/mount.just — the phase chain.
#
# Lives here rather than inline in the root justfile so the whole `sbx` surface
# sits in one directory, split from the in-sandbox `adw` layer by which
# credential it needs.
#
# Imported by the root justfile, so no `set` lines here.

# FLAGS forward to create, which records --target; fill pins a named target to
# its last pushed `just target sync`. --config is the one flag mount keeps for
# itself: it goes to setup, so gate C pings the roster this run will execute on.
# Without it, gate C pinged the default roster on every mount, whatever the arm
# (adversarial review 2026-10-02, 7.4). Pass the same path to `execute`.
# Stops at observe by design: teardown is ALWAYS an explicit separate decision.
# create -> fill -> setup -> observe, never teardown: just sbx mount <task> [--limit N] [--target NAME] [--config PATH]
mount RUN_ID *FLAGS:
    #!/usr/bin/env bash
    set -euo pipefail
    # $1 is RUN_ID, the variadic FLAGS follow (root sets positional-arguments).
    shift || true
    CONFIG=""; CREATE_FLAGS=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --config)   CONFIG="${2:-}"; shift 2 || { echo "mount: --config needs a path" >&2; exit 2; } ;;
            --config=*) CONFIG="${1#*=}"; shift ;;
            *) CREATE_FLAGS+=("$1"); shift ;;
        esac
    done
    # Fail before minting a key or booting a VM, not at gate C.
    if [ -n "$CONFIG" ] && [ ! -f "$CONFIG" ]; then
        echo "mount: no roster at '$CONFIG'" >&2; exit 2
    fi
    just sbx lifecycle create "{{RUN_ID}}" ${CREATE_FLAGS[@]+"${CREATE_FLAGS[@]}"}
    ID=$(sandbox_mount/host/run_record.py list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["run_id"])')
    just sbx lifecycle fill    "$ID"
    just sbx lifecycle setup   "$ID" "$CONFIG"
    just sbx lifecycle observe "$ID"
    printf '\nmounted: %s\n  execute: just sbx lifecycle execute %s "<prompt>"%s\n  agent:   just sbx run agent %s "<prompt>"\n  harvest: just sbx manage harvest %s\n  destroy: just sbx lifecycle teardown %s\n' "$ID" "$ID" "${CONFIG:+ \"$CONFIG\"}" "$ID" "$ID" "$ID"

