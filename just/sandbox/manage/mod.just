# just/sandbox/manage/mod.just — auxiliary: preflight, readback, fleet ops.
#
# Nothing here is a phase. None of it writes to the run record, and apart from
# `reap` (which revokes keys, deliberately) none of it changes anything: these
# are the commands you run to find out where you stand, before or after the
# lifecycle does its work.
#
# A module inherits nothing, so the settings are repeated here with one more `..`
# than the parent. dotenv-load matters in this one: `reap` cannot list or delete
# without OPENROUTER_PROVISIONING_KEY, and `doctor` checks for it.
set working-directory := '../../..'
set positional-arguments
set dotenv-load

import 'list.just'
import 'harvest.just'
import 'traces.just'
import 'reap.just'

# list the management commands
default:
    @just --list sbx::manage

# are the host prerequisites in place? run this before anything else
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    ok=0
    chk() { if eval "$2" >/dev/null 2>&1; then printf '  ok    %s\n' "$1"; else printf '  FAIL  %s\n' "$1"; ok=1; fi; }
    chk "ssh exe.dev reachable"            'ssh -o BatchMode=yes -o ConnectTimeout=15 exe.dev whoami'
    chk "OPENROUTER_PROVISIONING_KEY set"  '[ -n "${OPENROUTER_PROVISIONING_KEY:-}" ]'

    # A separate, non-generic check: a bare pass/fail here would hide WHY, and this
    # exact failure mode (right shape, wrong key type — a regular API key, not a
    # management key) already cost real setup time once. See NEXTSTEPS.md.
    if [ -n "${OPENROUTER_PROVISIONING_KEY:-}" ]; then
        CODE=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' https://openrouter.ai/api/v1/keys \
               -H "Authorization: Bearer $OPENROUTER_PROVISIONING_KEY" 2>/dev/null || echo "000")
        case "$CODE" in
            200) printf '  ok    OPENROUTER_PROVISIONING_KEY authorizes /api/v1/keys\n' ;;
            401|403)
                printf '  FAIL  OPENROUTER_PROVISIONING_KEY rejected by /api/v1/keys (HTTP %s)\n' "$CODE"
                printf '        this is almost always a REGULAR API key, not a management key.\n'
                printf '        mint one at https://openrouter.ai/settings/management-keys (NOT /settings/keys)\n'
                ok=1 ;;
            *)   printf '  FAIL  /api/v1/keys returned HTTP %s (expected 200)\n' "$CODE"; ok=1 ;;
        esac
    fi

    chk "run_record helper runs"           'sandbox_mount/host/run_record.py list'
    chk "provisioner present"              '[ -x sandbox_mount/guest/provision.sh ]'
    chk "models template has rates"        'python3 -c "import json;m=json.load(open(\"sandbox_mount/guest/models.json.tmpl\"))[\"providers\"][\"openrouter\"][\"models\"];assert all(set(x[\"cost\"])=={\"input\",\"output\",\"cacheRead\",\"cacheWrite\"} for x in m)"'
    chk "adw layer resolves"               'just --list adw'
    [ "$ok" -eq 0 ] && echo "  sbx doctor: OK" || { echo "  sbx doctor: FAILED"; exit 1; }
