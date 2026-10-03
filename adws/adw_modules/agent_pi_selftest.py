"""Offline check: an exception out of pi's read loop must not orphan pi.

    uv run --with pydantic --with pyyaml --with python-dotenv --with rich python -m adws.adw_modules.agent_pi_selftest

Ctrl-C becomes SystemExit inside the read loop (session.py's handler), and so
does any exception raised by an `on_event` callback. pi runs in its own
process group (start_new_session), so the terminal's signal never reaches it:
unless the loop kills the group on the way out, pi keeps spending and editing
while the trace says it is gone. A fake pi spawns a grandchild `sleep`, emits
one event, then hangs; the callback raises SystemExit. Both pids must be dead.
No model, no network.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from . import agent_pi
from .data_types import PiRequest

FAKE = """#!/usr/bin/env bash
sleep 300 &
echo $$ $! > "{pids}"
echo '{{"type":"agent_start"}}'
sleep 300
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pi-selftest-"))
    pids_file = tmp / "pids"
    fake = tmp / "pi"
    fake.write_text(FAKE.format(pids=pids_file))
    fake.chmod(0o755)

    agent_pi.PI_PATH = str(fake)
    agent_pi.resolve_model = lambda pattern: ("fake", "fake")
    agent_pi.context_window = lambda provider, model_id: 1

    def boom(_event: dict) -> None:
        raise SystemExit(130)          # what session.py's SIGINT handler raises

    request = PiRequest(prompt="x", system_prompt="x", model="fake", session_id="s",
                        session_dir=str(tmp), raw_output_path=str(tmp / "raw.jsonl"), cwd=str(tmp))
    raised = False
    try:
        agent_pi.run(request, on_event=boom)
    except SystemExit:
        raised = True

    pi_pid, child_pid = (int(x) for x in pids_file.read_text().split())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (alive(pi_pid) or alive(child_pid)):
        time.sleep(0.1)
    leaked = [p for p in (pi_pid, child_pid) if alive(p)]
    for pid in leaked:                 # never leave the test's own orphans behind
        os.kill(pid, 9)

    # The ordinary path must be untouched: a pi that answers and exits returns its text.
    clean = tmp / "pi-clean"
    clean.write_text("#!/usr/bin/env bash\n"
                     "echo '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\","
                     "\"content\":[{\"type\":\"text\",\"text\":\"done\"}],\"usage\":{}}}'\n")
    clean.chmod(0o755)
    agent_pi.PI_PATH = str(clean)
    result = agent_pi.run(request.model_copy(update={"raw_output_path": str(tmp / "raw2.jsonl")}))
    normal = result.returncode == 0 and result.text == "done"

    ok = raised and not leaked and normal
    print(f"agent_pi selftest: raised={raised} pi={pi_pid} child={child_pid} "
          f"leaked={leaked or 'none'} normal_exit={'ok' if normal else repr(result.text)} "
          f"-> {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
