# SHOT. Screenshot a sandbox's live app and record its console errors.
#
# WHY. The agents on the VM read source, not pixels. Nothing in the chain can
# tell you what was actually delivered — on 2026-09-18 two arms had green tests,
# zero type errors and a reviewer's partial sign-off, and threw a TypeError on
# page load. One look at the page said more than either envelope did.
#
# NOT A GATE, and deliberately so. `happy-dom` in the fixed suite now catches
# render crashes inside the fix loop, which is the right place for a gate — it
# can still be fixed there. This is the RECORD of what shipped, captured before
# teardown destroys the VM and the URL with it. It exits 0 even on a crashed
# page: the crash is the finding, not a reason to fail the command.
#
# HOST-SIDE ON PURPOSE. Chromium is ~190 MB; putting it on every arm's VM buys
# nothing happy-dom does not already cover, and costs provisioning time on every
# mount. The host already has a browser.
#
# Run it AFTER `just sbx lifecycle refresh <run-id>`, or you are photographing a
# stale bundle.

# screenshot a run's live app: just sbx manage shot <run-id> [--full] [--json]
shot RUN_ID *FLAGS:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{ justfile_directory() }}"

    RR="sandbox_mount/host/run_record.py"
    RUN_ID="{{ RUN_ID }}"

    "$RR" get "$RUN_ID" > /dev/null || { echo "!! shot: no run record for $RUN_ID" >&2; exit 1; }
    URL=$("$RR" get "$RUN_ID" https_url)
    [ -n "$URL" ] || { echo "!! shot: no https_url in the record — has OBSERVE run?" >&2; exit 1; }

    OUT=".sandbox/shots/${RUN_ID}.png"
    mkdir -p .sandbox/shots

    echo "── shot $RUN_ID  $URL"
    # `uv run --with playwright` keeps this off the host's global env, matching how
    # every other host helper here borrows its dependencies.
    uv run --with playwright sandbox_mount/host/shoot_app.py "$URL" "$OUT" {{ FLAGS }}
