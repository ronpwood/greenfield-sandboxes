"""Offline check of the `session:` roster key: who shares a pi session, and who refuses to.

    uv run --with pydantic --with pyyaml --with python-dotenv --with rich python -m adws.adw_modules.session_share_selftest

Three claims, each against the real team-sonnet roster with sessions added in memory:

  share    a second agent naming the same key rejoins the first one's session id,
           read from agent_map exactly as a live run writes it
  isolate  an agent with no key, or another key, gets a session of its own
  refuse   validate() fails a shared key whose agents differ in model

Exit 1 on any failure, naming it. Needs no VM, no model, no network.
See specs/fold1-single-loop-authoring.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from . import agents

ROSTER = "adws/adw_sssf_config/sssf.team-sonnet.config.yaml"
REQUIRED = ["planner", "test_designer", "builder", "reviewer", "documenter"]


def main() -> int:
    failures: list[str] = []
    cfg = agents.load_config(ROSTER)
    by_name = {a.name: a for a in cfg.agents}
    for name in ("planner", "test_designer", "builder"):
        by_name[name].session = "author"

    run = SimpleNamespace(adw_id="selftest", agent_map={})

    def opens(name: str) -> str:
        """Resolve a session id, then record it the way agents.execute() does."""
        agent = by_name[name]
        sid = agents._agent_session_id(run, agent)
        run.agent_map[agent.name] = {"session_id": sid, "model": agent.model,
                                     "coding_agent": agent.coding_agent,
                                     "session": agent.session}
        return sid

    planner = opens("planner")
    if opens("test_designer") != planner:
        failures.append("share: test_designer did not rejoin the planner's session")
    if opens("builder") != planner:
        failures.append("share: builder did not rejoin the planner's session")
    if opens("builder") != planner:
        failures.append("share: builder's own re-entry (fix/revise) left the shared session")
    if opens("reviewer") == planner:
        failures.append("isolate: the reviewer joined the author session")

    # pi resolves --session-id INSIDE --session-dir: a shared id under per-agent
    # dirs opens a fresh session each time (the 2026-10-29 smoke shipped exactly that).
    run.session_dir = Path("/tmp/sssf-selftest")
    dirs = {name: agents._pi_session_dir(run, by_name[name])
            for name in ("planner", "test_designer", "builder", "reviewer")}
    if len({dirs["planner"], dirs["test_designer"], dirs["builder"]}) != 1:
        failures.append(f"share: session-mates get different pi session dirs: {dirs}")
    if dirs["reviewer"] == dirs["planner"]:
        failures.append("isolate: the reviewer's pi session dir is the author's")
    if dirs["reviewer"] != str((run.session_dir / "reviewer" / "pi_sessions").resolve()):
        failures.append(f"isolate: an unshared agent's pi session dir moved: {dirs['reviewer']}")

    try:
        agents.validate(cfg, REQUIRED)
    except SystemExit as e:
        failures.append(f"refuse: a valid shared roster failed validate(): {e}")

    by_name["test_designer"].model = "openrouter/anthropic/claude-opus-5.5"
    try:
        agents.validate(cfg, REQUIRED)
        failures.append("refuse: validate() accepted session-mates with different models")
    except SystemExit as e:
        if "share session" not in str(e):
            failures.append(f"refuse: validate() failed, but not on the shared session: {e}")

    for f in failures:
        print(f"FAIL {f}")
    print(f"session_share_selftest: {'FAIL' if failures else 'ok'} "
          f"({len(failures)} failure(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
