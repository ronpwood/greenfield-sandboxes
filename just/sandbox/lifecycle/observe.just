# ─── just/sandbox/observe.just — PHASE 5 · OBSERVE ────────────────────────────
# Make a mounted sandbox watchable from outside without reaching in: start the
# app (dev server on loopback :4502, Host-rewriting proxy on *:4501) and the
# trace UI (4600), retarget the HTTPS proxy, print URLs.
#
# IMPORTED by the root justfile, not a module: no `set` lines here (the root
# already owns `set dotenv-load` / `set positional-arguments`, and a duplicate
# setting is a hard error), and the root's variable scope is shared.
#
# HOST-ONLY in practice: this phase touches the exe.dev control plane, which a
# sandbox has no credential for. It needs NO
# secrets at all — no OpenRouter key of either kind is read or forwarded here.
#
# NEVER destroys the VM. Every failure below reports and exits non-zero with the
# box still up, because the evidence you need lives on it. `just sbx lifecycle teardown` stays
# an explicit, separate decision.

# Phase 5 · start both servers if down, expose 4501 anonymously, print the URLs
observe RUN_ID:
    #!/usr/bin/env bash
    set -euo pipefail

    RR="sandbox_mount/host/run_record.py"

    # The run record is the ONLY state shared across the six phases; nothing
    # else survives between them.
    VM=$("$RR" get {{RUN_ID}} vm_name)
    if [ -z "$VM" ]; then
        echo "observe: run record {{RUN_ID}} has no vm_name — run 'just sbx lifecycle create' first" >&2
        exit 1
    fi
    HOST="$VM.exe.xyz"

    APP_PORT=4501            # public: the proxy's wildcard bind, what exe.dev forwards to
    APP_UPSTREAM_PORT=4502   # internal: the app's dev server, loopback only
    OBS_PORT=4600
    # The app's name and dir come from the run's TARGET (recorded by CREATE):
    # the VM's app/ is a clone of that target's source.repo, and the target's
    # manifest declares its relative app.dir.
    TARGET=$("$RR" get {{RUN_ID}} target)
    APP_NAME=$(uv run adws/adw_modules/manifest.py --target "$TARGET" get app.name)
    APP_REL=$(uv run adws/adw_modules/manifest.py --target "$TARGET" get app.dir)
    # The clone target is `app` relative to the exedev user's home, so this is
    # /home/exedev/app/<app.dir> on a stock exeuntu VM. $HOME stays literal
    # here and expands on the VM, not on the host.
    APP_DIR='$HOME/app/'"$APP_REL"
    OBS_DIR='$HOME/app/.claude/skills/sssf/apps/visualizer'
    OBS_DB='$HOME/app/adws/adw_data/sssf.db'
    PROXY='$HOME/app/sandbox_mount/guest/app_proxy.ts'

    echo "── observe {{RUN_ID}} · vm $VM ──────────────────────────────"

    # ── 0. reachability ──────────────────────────────────────────────────────
    # Proven up front so that every `ss` probe below can read a non-zero exit as
    # "not listening" rather than "ssh is broken". All *.exe.xyz share one host
    # key and a wildcard known_hosts entry is already installed, so this is
    # non-interactive.
    # `< /dev/null` on every LOCAL ssh below — see create.just. The redirects already
    # inside the nohup command strings detach the REMOTE process; these detach ssh
    # itself from the caller's stdin, which is what keeps a fan-out loop alive.
    if ! ssh "$HOST" 'true' < /dev/null >/dev/null 2>&1; then
        echo "observe: cannot ssh $HOST — VM left up for debugging" >&2
        exit 1
    fi
    echo "[1/6] ssh ok — $HOST"

    # `ss -ltn` column 4 is Local Address:Port. The dev server is loopback-only
    # BY DESIGN (127.0.0.1:4502 or [::1]:4502 — bun's default bind, true of
    # 1.3.x and 1.4.x alike); the proxy is the one wildcard bind (*:4501). The
    # :PORT suffix match catches both shapes, so this probe says "something
    # listens on that port", not where — the where is app_proxy.ts's job.
    listening() {
        ssh "$HOST" 'ss -ltn' < /dev/null 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    }

    # Poll rather than sleep-a-guess: bun is up in well under a second warm, but
    # a cold first read of dist/ is slower.
    wait_listen() {
        local port="$1" tries=0
        until listening "$port"; do
            tries=$((tries + 1))
            [ "$tries" -ge 20 ] && return 1
            sleep 1
        done
        return 0
    }

    # ── 1. app: dev server on loopback, proxy on the public port ─────────────
    # Idempotent: observe is safe to re-run, and `just sbx mount` ends here, so a
    # second call must not stack a second process on either port.
    #
    # TWO processes, by design. bun's HTML dev server carries a DNS-rebinding
    # guard (DevServer.rs, is_allowed_host_header) that 403s any Host that is not
    # localhost / *.localhost / an IP literal / its configured hostname, and
    # <vm>.exe.xyz is none of those. It has no allow-list knob. So the dev server
    # stays where bun wants it — default loopback bind, live rebundling — on
    # APP_UPSTREAM_PORT, and sandbox_mount/guest/app_proxy.ts on APP_PORT is the
    # only wildcard bind on the box, forwarding with Host rewritten to localhost.
    # That is the one rule the guard can never remove, so this survives bun
    # releases that a flag would not. See specs/toolchain-unpin-and-drift-visibility.md.
    #
    # The listening() guard below means a re-run NEVER restarts a live server,
    # so the dev server here is always the one from mount time. After an ADW
    # commits, bun's incremental rebundle can serve a broken module graph; use
    # `just sbx lifecycle refresh <id>` to bounce the dev server alone. That
    # phase exists precisely so this one can stay idempotent.
    #
    # PER-APP boot command: the current app is a static Bun HTML entry, not a
    # server.ts — PORT must be passed explicitly, since Bun's HTML dev server
    # defaults to :3000. An app swap that isn't a static HTML entry rewrites
    # `bun index.html` below (same rationale as quality.py's command blocks:
    # paths are data, commands are stack-specific judgement). An app swap that
    # ships its OWN server sets APP_UPSTREAM_PORT to wherever that server
    # listens and leaves the proxy alone — the proxy is app-agnostic.
    echo "[2/6] app  :$APP_PORT (proxy) <- :$APP_UPSTREAM_PORT (dev server)"
    # start_bg <port> <log basename> <remote cwd> <remote command>
    # Each port is guarded by listening() separately: a re-run where only the
    # proxy died restarts only the proxy.
    start_bg() {
        local port="$1" log="$2" dir="$3" cmd="$4"
        if listening "$port"; then
            echo "       :$port already listening — leaving it alone"
            return 0
        fi
        # All three detachment pieces are REQUIRED or ssh never returns: nohup
        # (survive session end), the redirect (release stdout/stderr), and
        # < /dev/null (release stdin). Dropping any one hangs this recipe.
        ssh "$HOST" "cd $dir && ( nohup $cmd > \$HOME/$log 2>&1 < /dev/null & echo \$! )" < /dev/null \
            | sed "s/^/       :$port started pid /"
        if ! wait_listen "$port"; then
            echo "observe: nothing bound :$port — tail of ~/$log:" >&2
            ssh "$HOST" "tail -n 40 \$HOME/$log" < /dev/null >&2 || true
            exit 1
        fi
        echo "       :$port up"
    }
    start_bg "$APP_UPSTREAM_PORT" "${APP_NAME}-app.log"   "$APP_DIR"   "env PORT=$APP_UPSTREAM_PORT bun index.html"
    start_bg "$APP_PORT"          "${APP_NAME}-proxy.log" '$HOME/app' "env PORT=$APP_PORT UPSTREAM=$APP_UPSTREAM_PORT bun $PROXY"

    # ── 2. visualizer on 4600 ────────────────────────────────────────────────
    # VERIFIED in the plan: the visualizer EXITS when sssf.db is missing. Check
    # for the db before spending a start attempt, so the error names the real
    # cause instead of showing an empty port.
    echo "[3/6] obs  :$OBS_PORT"
    if ! ssh "$HOST" "test -f $OBS_DB" < /dev/null; then
        echo "observe: $OBS_DB is missing on $VM." >&2
        echo "         The visualizer exits immediately without it. That db is created by" >&2
        echo "         sandbox_mount/guest/provision.sh (step 8, 'trace db') — re-run SETUP:" >&2
        echo "           just sbx lifecycle setup {{RUN_ID}}" >&2
        exit 1
    fi
    if listening "$OBS_PORT"; then
        echo "       already listening — leaving it alone"
    else
        # SSSF_DB goes BEFORE nohup, not after: `nohup VAR=x cmd` makes nohup
        # try to exec a program literally named "VAR=x". As a prefix on nohup
        # the assignment lands in its environment and bun inherits it.
        # The db path is passed explicitly because it resolves against the
        # server's cwd, and the server runs from the visualizer app dir.
        ssh "$HOST" "cd $OBS_DIR && ( SSSF_DB=$OBS_DB nohup bun run server/index.ts > \$HOME/visualizer.log 2>&1 < /dev/null & echo \$! )" < /dev/null \
            | sed 's/^/       started pid /'
        if ! wait_listen "$OBS_PORT"; then
            echo "observe: visualizer never bound :$OBS_PORT — tail of ~/visualizer.log:" >&2
            ssh "$HOST" 'tail -n 40 $HOME/visualizer.log' < /dev/null >&2 || true
            echo "         If it says 'sssf.db not found', SETUP did not initialize the trace db:" >&2
            echo "           sandbox_mount/guest/provision.sh  ->  just sbx lifecycle setup {{RUN_ID}}" >&2
            exit 1
        fi
        echo "       up"
    fi

    # ── 3. proxy ─────────────────────────────────────────────────────────────
    # MANDATORY. A fresh exeuntu VM proxies port 8000 (smallest EXPOSEd port
    # >= 1024); without this retarget https://$HOST/ hits nothing. set-public
    # applies to the primary port only — see the note printed at the end.
    echo "[4/6] proxy -> :$APP_PORT, public"
    ssh exe.dev share port "$VM" "$APP_PORT" < /dev/null
    ssh exe.dev share set-public "$VM" < /dev/null

    # ── 4. record ────────────────────────────────────────────────────────────
    echo "[5/6] recording ports"
    "$RR" set {{RUN_ID}} "ports={\"app\":$APP_PORT,\"obs\":$OBS_PORT}"

    # ── 5. verify ────────────────────────────────────────────────────────────
    # curl with no auth of any kind — that is the whole point of the app check.
    # No -L: a redirect is a result here, not something to follow.
    echo "[6/6] verifying"
    code_of() {
        curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$1" || echo 000
    }

    # The public flag can lag the API call by a beat; retry before failing.
    APP_CODE=000
    for _ in 1 2 3 4 5 6; do
        APP_CODE=$(code_of "https://$HOST/")
        [ "$APP_CODE" = "200" ] && break
        sleep 2
    done
    if [ "$APP_CODE" != "200" ]; then
        echo "observe: app returned $APP_CODE anonymously at https://$HOST/ (want 200)" >&2
        echo "         VM left up. Check: ssh exe.dev share show $VM" >&2
        exit 1
    fi
    echo "       app  200 anonymous"

    # 307/401 anonymously is CORRECT for the obs port: alternate ports stay
    # auth-gated to users with VM access. Only a dead proxy (000) or a 5xx is a
    # real failure; anything else means the port is reachable and gated.
    OBS_CODE=$(code_of "https://$HOST:$OBS_PORT/")
    if [ "$OBS_CODE" = "000" ] || [ "$OBS_CODE" -ge 500 ]; then
        echo "observe: obs port unreachable at https://$HOST:$OBS_PORT/ (got $OBS_CODE)" >&2
        exit 1
    fi
    echo "       obs  $OBS_CODE anonymous — expected, it is owner-gated"

    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  app  https://$HOST/"
    echo "  obs  https://$HOST:$OBS_PORT/"
    echo "════════════════════════════════════════════════════════════"
    echo "  The obs URL opens fine in a browser already signed in to exe.dev."
    echo "  Ports 3000-9999 are auto-forwarded by the proxy but auth-gated;"
    echo "  only ONE port can ever be anonymous, and that is $APP_PORT."
