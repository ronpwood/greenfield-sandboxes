# TRACE METRICS. Count how hard a run's agents fought their tools.
#
# `traces` brings the thinking home; this reads it. The one number it exists to
# surface: on gf-e2e-20260917-cbb166 the builder made 128 tool calls and errored
# on 20 of them, 18 being the same malformed `edit` call. That cost 12.0M tokens
# of thrash and appeared NOWHERE in run.log, the envelopes or the cost report.
#
# READS PULLED TRACES ONLY, never the VM. A live run's raw_output.jsonl is being
# appended to as you read it, so a number taken from it is not reproducible —
# pull first, measure second, and the same bytes give the same answer next week.
# That is also why this works fine after teardown, when the VM is long gone.

# per-agent tool calls, errors and error kinds: just sbx manage trace-metrics <run-id> [--json]
trace-metrics RUN_ID *FLAGS:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{ justfile_directory() }}"

    DEST=".sandbox/traces/{{ RUN_ID }}"
    # The hint IS the error: a missing traces dir almost always means the pull was
    # never run (or ran before the agents finished), not that the run had no tools.
    if [ ! -d "$DEST" ]; then
        echo "!! trace-metrics: no traces pulled for {{ RUN_ID }} ($DEST)" >&2
        echo "   run: just sbx manage traces {{ RUN_ID }}" >&2
        exit 1
    fi

    sandbox_mount/host/trace_metrics.py "$DEST" {{ FLAGS }}
