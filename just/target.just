# just/target.just — the `target` namespace: named codebases a sandbox can mount.
#
# Loaded from the root justfile:
#     mod target 'just/target.just'
#
# The root app.manifest.yaml is the implicit `default` target. Every
# targets/<name>.yaml is another: a separate clean-room repo with the same factory
# and a different payload (e.g. `greenfield`, an empty app shell). The flow:
#
#     just target sync greenfield --push          regenerate its factory from HEAD, push, pin
#     just sbx mount <id> --target greenfield     VM clones it at the pinned sync
#     just sbx lifecycle execute <id> prompts/greenfield.md "" tdd
#     just sbx manage harvest <id>                commits land in the target's checkout
#
# A module inherits NOTHING from its parent — cwd defaults to this file's
# directory, hence `set working-directory`. Recipes use bash shebangs and pass
# arguments positionally ("$@"), never interpolated into shell text.
set working-directory := '..'
set positional-arguments
set dotenv-load

# list the target commands
default:
    @just --list target

# list known targets: default + every targets/*.yaml
list:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run -q adws/adw_modules/manifest.py list

# print a target's definition and its last sync provenance: just target show greenfield
show NAME:
    #!/usr/bin/env bash
    set -euo pipefail
    name="$1"
    if [ "$name" = "default" ]; then
        echo "# default target — app.manifest.yaml"; cat app.manifest.yaml; exit 0
    fi
    # manifest.py validates the name (no paths) and lists the known ones on a miss.
    uv run -q adws/adw_modules/manifest.py --target "$name" get app.name > /dev/null
    echo "# targets/$name.yaml"
    cat "targets/$name.yaml"
    echo
    # The guard lives in target_sync.py; this only reports its first line.
    # Exit 0 prints "pristine: ok (...)"; exit 5 prints the one-line summary.
    if out=$(uv run -q sandbox_mount/host/target_sync.py "$name" --check-pristine 2>&1); then
        echo "$out"
    else
        echo "$out" | head -1 | sed 's/^target sync: target not pristine/pristine: DIRTY/'
        echo "  (details: uv run sandbox_mount/host/target_sync.py $name --check-pristine)"
    fi
    prov=".sandbox/targets/$name.json"
    if [ -f "$prov" ]; then
        echo "# $prov (last sync)"
        cat "$prov"
        host_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["host_sha"])' "$prov")
        if [ "$host_sha" = "$(git rev-parse HEAD)" ]; then
            echo "factory: synced from current HEAD"
        else
            echo "factory: synced from ${host_sha:0:12}; HEAD is $(git rev-parse --short=12 HEAD) — re-sync if factory paths changed"
        fi
    else
        echo "# never synced — just target sync $name --dry-run"
    fi

# regenerate a target repo's factory from HEAD: just target sync greenfield [--dry-run] [--push]
sync NAME *FLAGS:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run sandbox_mount/host/target_sync.py "$@"
