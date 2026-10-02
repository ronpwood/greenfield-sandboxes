"""Offline check that no agent, on any roster, may write what it is judged by.

    uv run --with pydantic --with pyyaml python -m adws.adw_modules.permissions_selftest

Walks every adws/adw_sssf_config/*.yaml and asks `permissions.permitted()` about
a fixed set of paths for every agent. Two kinds of row:

  hard     the same answer for every agent, whatever its `writes` says — the
           session runtime is always writable; the prompt sets, the harness and
           the factory's code never are
  derived  the answer follows from the agent's own `writes` — `None` may write
           app code and both suites (the builder grows the fixed suite and
           corrects red values under amendments, by design), a named lane
           unlocks it, `[]` may write nothing in the repo

Suite paths come from the manifest, so an app swap keeps this valid. Exit 1 on
any wrong cell, naming it. Needs no VM, no model, no network.
"""

from __future__ import annotations

import glob
import sys

import yaml

from . import permissions
from .data_types import SSSFConfig
from .manifest import load as load_manifest


def main() -> int:
    app = load_manifest().app
    generated = app.generated_tests_dir.rstrip("/") + "/x.test.ts"
    app_code = app.dir.rstrip("/") + "/main.ts"

    def names(agent, path: str) -> bool:
        return any(permissions._matches(path, p) for p in (agent.writes or []))

    def free(agent, path: str) -> bool:
        return agent.writes is None or names(agent, path)

    rows = [
        # (path, expected(agent) -> bool)
        ("adws/adw_data/sessions/x/context_handoff/review.md", lambda a: True),
        ("adws/adw_data/prompt_engineering_team/reviewer/system.md", lambda a: False),
        ("adws/adw_data/prompt_engineering/builder/system.md", lambda a: False),
        ("adws/adw_data/harness_engineering/bash_timeout.ts", lambda a: False),
        ("adws/adw_data/fixtures/team_spec/good.md", lambda a: False),
        (app.test_file, lambda a: free(a, app.test_file)),
        ("adws/adw_modules/gates.py", lambda a: False),
        (generated, lambda a: free(a, generated)),
        (app_code, lambda a: free(a, app_code)),
        ("specs/x.md", lambda a: free(a, "specs/x.md")),
    ]

    wrong, total = [], 0
    for path_cfg in sorted(glob.glob("adws/adw_sssf_config/*.yaml")):
        cfg = SSSFConfig(**yaml.safe_load(open(path_cfg)))
        roster = path_cfg.rsplit("/", 1)[-1]
        for agent in cfg.agents:
            for path, expected in rows:
                total += 1
                want, got = expected(agent), permissions.permitted(path, agent, cfg)
                if want != got:
                    wrong.append(f"  {roster:30} {agent.name:14} {path}: "
                                 f"permitted={got}, want {want}")

    if wrong:
        print(f"permissions selftest: {len(wrong)} of {total} cells WRONG")
        print("\n".join(wrong))
        return 1
    print(f"permissions selftest: {total} cells OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
