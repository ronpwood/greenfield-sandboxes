# ─── just/sandbox/lifecycle/refresh.just — PHASE 5b · REFRESH ────────────────
# Rebuild the review URL's bundle from scratch by bouncing ONLY the app's dev
# server. The proxy, the visualizer, and the exe.dev port exposure are all left
# untouched.
#
# IMPORTED by lifecycle/mod.just, so no `set` lines and no top-level variables:
# the phase files share one scope and a duplicate name is a hard error.
#
# ── Why this phase exists ────────────────────────────────────────────────────
# bun's HTML dev server rebundles on request when files change underneath it,
# and for hand-edits that works — HMR connects through the proxy and reloads.
# But when a whole ADW commit lands at once (five files rewritten by an agent
# while the watcher is running), bun 1.4.2's INCREMENTAL rebundle can emit a
# bundle with a broken module registry. The browser then shows:
#
#     Failed to load bundled module './main.ts'. This is not a dynamic import,
#     and therefore is a bug in Bun's bundler.
#
# Measured on run drift-day2-20260907-c3da02, adw 0eb03b9e, reproduced twice in
# each direction by rolling apps/ back and forward under the running watcher:
#
#     fresh server start   ->  114,814 byte chunk  ->  renders, console clean
#     incremental rebundle ->  114,799 byte chunk  ->  runtime error above
#
# Same commit, same bun, same server — only the bundling path differs. So the
# fix is not "wait longer" or "hard-reload the browser": the bytes on the wire
# are wrong and only a fresh process regenerates them.
#
# This matters more than it looks. main.ts calls init() at module scope, so
# `bun test` cannot import it — the browser IS the only gate on main.ts. A
# review URL that lies about main.ts means that gate is not real.
#
# ── Why observe can't just do this ───────────────────────────────────────────
# observe's start_bg() is guarded by listening() ON PURPOSE: observe is safe to
# re-run and `just sbx mount` ends with it, so it must never stack a second
# process on a port. That guard is correct, and it is also why the dev server
# serving your review URL is ALWAYS the one started at mount time — before the
# ADW ran. Rather than weaken observe's idempotency, this phase is the explicit
# "I want new bytes" verb.
#
# ── The trap this recipe exists to avoid ─────────────────────────────────────
# Doing this by hand over ssh goes wrong in a way that looks like it worked.
# If a new dev server starts while the old one still holds APP_UPSTREAM_PORT,
# bun does NOT fail — it silently binds the next free port (4503) and logs it,
# while the proxy keeps forwarding to the stale process on 4502. The chunk hash
# never changes and the "restart" appears to do nothing. So: kill everything,
# WAIT for the port to actually free, start exactly one, then prove there is
# exactly one and that it landed on the port we asked for.

# Phase 5b · bounce ONLY the app dev server so the review URL serves a fresh bundle
refresh RUN_ID:
    #!/usr/bin/env bash
    set -euo pipefail

    RR="sandbox_mount/host/run_record.py"

    VM=$("$RR" get {{RUN_ID}} vm_name)
    if [ -z "$VM" ]; then
        echo "refresh: run record {{RUN_ID}} has no vm_name — run 'just sbx lifecycle create' first" >&2
        exit 1
    fi
    HOST="$VM.exe.xyz"

    # Must match observe.just. If you change a port or the boot command there,
    # change it here too — an app swap touches both files.
    APP_PORT=4501
    APP_UPSTREAM_PORT=4502
    # Resolved from the run's recorded target, exactly as observe.just does.
    TARGET=$("$RR" get {{RUN_ID}} target)
    APP_NAME=$(uv run adws/adw_modules/manifest.py --target "$TARGET" get app.name)
    APP_REL=$(uv run adws/adw_modules/manifest.py --target "$TARGET" get app.dir)
    APP_DIR='$HOME/app/'"$APP_REL"

    # PER-APP boot command — the same judgement call observe.just documents: the
    # current app is a static Bun HTML entry, so PORT must be passed explicitly
    # (bun's HTML dev server defaults to :3000). An app that ships its own
    # server rewrites this line and observe's to match.
    BOOT="env PORT=$APP_UPSTREAM_PORT bun index.html"
    # The bracket on [b]un is load-bearing: a bare `pkill -f "bun index.html"`
    # matches the ssh command string carrying the pattern and kills its own
    # shell. The character class cannot match itself.
    PATTERN='[b]un index.html'
    # `pgrep -f` also matches shell wrappers that happen to carry the boot
    # command in their argv (`bash -c cd … && … bun index.html …`). Counting
    # those would report 2 servers where there is 1. `pgrep -a` prints
    # "PID command args", so field 2 is the executable — keep only rows where
    # that is bun itself.
    COUNT_CMD="pgrep -af '$PATTERN' 2>/dev/null | awk '\$2 == \"bun\"' | wc -l | tr -d ' '"

    echo "── refresh {{RUN_ID}} · vm $VM ──────────────────────────────"

    if ! ssh "$HOST" 'true' >/dev/null 2>&1; then
        echo "refresh: cannot ssh $HOST — VM left up for debugging" >&2
        exit 1
    fi
    echo "[1/5] ssh ok — $HOST"

    listening() {
        ssh "$HOST" 'ss -ltn' 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    }

    # ── 1. stop every dev server ─────────────────────────────────────────────
    # Every one, not "the" one: the whole failure mode this guards against is a
    # duplicate that bound a different port and is invisible on 4502.
    echo "[2/5] stopping dev server(s) on :$APP_UPSTREAM_PORT"
    BEFORE=$(ssh "$HOST" "$COUNT_CMD" || true)
    echo "       found ${BEFORE:-0} running"
    ssh "$HOST" "pkill -f '$PATTERN' || true"

    # Wait for the PORT, not the process: a dying bun can hold the listener for
    # a beat after pkill returns, and starting into that window is exactly how
    # the silent fallback to 4503 happens.
    FREED=""
    for _ in $(seq 1 20); do
        if ! listening "$APP_UPSTREAM_PORT"; then FREED=yes; break; fi
        sleep 1
    done
    if [ -z "$FREED" ]; then
        echo "refresh: :$APP_UPSTREAM_PORT still bound after pkill — refusing to start a second server" >&2
        echo "         (starting now would silently bind the next free port and change nothing)" >&2
        ssh "$HOST" "pgrep -af '$PATTERN'" >&2 || true
        exit 1
    fi
    echo "       :$APP_UPSTREAM_PORT free"

    # ── 2. start exactly one ─────────────────────────────────────────────────
    # Same three detachment pieces observe.just documents: nohup, the redirect,
    # and < /dev/null. Dropping any one hangs this recipe.
    echo "[3/5] starting one dev server"
    ssh "$HOST" "cd $APP_DIR && ( nohup $BOOT > \$HOME/${APP_NAME}-app.log 2>&1 < /dev/null & echo \$! )" \
        | sed 's/^/       started pid /'

    STARTED=""
    for _ in $(seq 1 20); do
        if listening "$APP_UPSTREAM_PORT"; then STARTED=yes; break; fi
        sleep 1
    done
    if [ -z "$STARTED" ]; then
        echo "refresh: nothing bound :$APP_UPSTREAM_PORT — tail of ~/${APP_NAME}-app.log:" >&2
        ssh "$HOST" "tail -n 40 \$HOME/${APP_NAME}-app.log" >&2 || true
        exit 1
    fi

    # ── 3. prove it is ONE server on the RIGHT port ──────────────────────────
    # bun prints the url it actually bound. If it fell back, that line names the
    # wrong port and the proxy is still pointed at a corpse — a silent success
    # is the one outcome this phase must never produce.
    echo "[4/5] verifying a single server on :$APP_UPSTREAM_PORT"
    COUNT=$(ssh "$HOST" "$COUNT_CMD" || true)
    if [ "${COUNT:-0}" != "1" ]; then
        echo "refresh: expected exactly 1 dev server, found ${COUNT:-0}" >&2
        ssh "$HOST" "pgrep -af '$PATTERN'" >&2 || true
        exit 1
    fi
    if ! ssh "$HOST" "grep -q 'localhost:$APP_UPSTREAM_PORT' \$HOME/${APP_NAME}-app.log"; then
        echo "refresh: the dev server did not report :$APP_UPSTREAM_PORT — it may have fallen back:" >&2
        ssh "$HOST" "head -n 6 \$HOME/${APP_NAME}-app.log" >&2 || true
        exit 1
    fi
    echo "       1 process, bound to :$APP_UPSTREAM_PORT"

    # ── 4. prove the PUBLIC url serves the new bundle ────────────────────────
    # The page alone is not enough: index.html is served fine even when the
    # module graph is broken — that was the whole symptom. Fetch the bundle
    # chunk the page references, which is the artifact that was corrupt.
    echo "[5/5] verifying the public URL serves a fresh bundle"
    APP_CODE=000
    for _ in 1 2 3 4 5 6; do
        APP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$HOST/" || echo 000)
        [ "$APP_CODE" = "200" ] && break
        sleep 2
    done
    if [ "$APP_CODE" != "200" ]; then
        echo "refresh: app returned $APP_CODE anonymously at https://$HOST/ (want 200)" >&2
        echo "         The dev server is up, so suspect the proxy on :$APP_PORT or the exe.dev share:" >&2
        echo "           just sbx lifecycle observe {{RUN_ID}}" >&2
        exit 1
    fi
    CHUNK=$(curl -s --max-time 20 "https://$HOST/" | grep -oE '/_bun/client/[^"]+' | head -1 || true)
    if [ -z "$CHUNK" ]; then
        echo "refresh: served 200 but the page references no /_bun/client chunk — is this still a bun HTML app?" >&2
        exit 1
    fi
    CHUNK_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$HOST$CHUNK" || echo 000)
    CHUNK_SIZE=$(curl -s -o /dev/null -w '%{size_download}' --max-time 20 "https://$HOST$CHUNK" || echo 0)
    if [ "$CHUNK_CODE" != "200" ]; then
        echo "refresh: bundle chunk $CHUNK returned $CHUNK_CODE (want 200)" >&2
        exit 1
    fi
    echo "       app  200 anonymous"
    echo "       $CHUNK  200  $CHUNK_SIZE bytes"

    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  app  https://$HOST/   (fresh bundle)"
    echo "════════════════════════════════════════════════════════════"
    echo "  A fresh bundle only fixes bytes. It cannot fix code — if the"
    echo "  runtime error survives a refresh, it is the app, not the bundler."
