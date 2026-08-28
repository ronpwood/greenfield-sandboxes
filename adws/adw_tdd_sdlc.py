#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW TDD SDLC — plan, design failing tests, build to green, review, document.

Usage:
    uv run adws/adw_tdd_sdlc.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> planner -> git(commit_plan)
        -> test_designer [gates: artifacts_exist, tests_red]
        -> git(commit_tests)
        -> builder -> code(test: fixed + generated) [-> builder(fix) ... bounded]
        -> reviewer [-> builder(revise) -> reviewer ... bounded]
        -> code(retest, only if a revision changed code)
        -> git(commit_build) -> code(changes) -> documenter -> git(commit_docs)

Four commits, four work products, four authors. The plan, the tests, the code,
and the write-up each land in their own commit, in the words of the agent that
produced it. `adw_simple_sdlc.py` is this chain minus the test-design phase and
stays untouched as the control group: same prompt through both is the A/B
comparison this repo exists to run.

The novelty is the red gate. The test designer turns the plan into an
executable suite BEFORE the builder runs, and `gates.tests_red` proves the
suite is non-vacuous the only way that is mechanically checkable: it must FAIL
on the pre-build tree. A green smoke detector only proves it's on; you prove it
works by putting smoke under it. The red failure output rides to the builder in
the envelope as "make exactly this pass", and a failed gate travels back to the
same test-designer session as a correction, like every other gate violation.

From the builder on, the green bar always means BOTH suites — the fixed suite
(existing behavior survived) and the generated one (new behavior arrived) — in
one `bun test` command via `extra_files`. The fix loop is byte-identical to
the control chain's: "make red go green" is already what it does.

The code commit lands after verification, not straight after the build, and a
run that fails verification leaves the plan and the red tests committed with
the working tree dirty — the spec and the suite are real artifacts either way,
and a red suite with no code is exactly where a human picks TDD back up.

The documenter measures against the commit this run STARTED from, not against
`main`, because by then the run has moved `main` itself. That baseline is
pinned before the first commit phase and printed in the request phase.
"""

import argparse
import sys

from adw_modules import agents, changes, gates, git_helper, quality, session, utils
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    DocumentOutput, PhaseParams, PlanOutput,
                                    ReviewOutput, TestDesignOutput)

REQUIRED_AGENTS = ["planner", "test_designer", "builder", "reviewer", "documenter"]
MAX_FIX_LOOPS = 3
MAX_REVISION_LOOPS = 2

DOCUMENT_NOTES = ("Read diff_path in full before writing. Document only what the "
                  "diff shows, then copy the write-up into app_docs/ as your task "
                  "describes.")


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    baseline = git_helper.rev("HEAD")     # pinned before this run commits anything

    def commit(ph, envelope) -> None:
        """Commit what the preceding phase produced, in that agent's own words."""
        message = envelope.commit_message or f"sssf({run.adw_id}): {envelope.summary}"
        ph.log(sha=git_helper.commit_all(message), message=message)

    def record(ph, result) -> None:
        """Log a deterministic block's verdict — the same shape every ADW uses."""
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt, baseline=git_helper.short_sha(baseline))

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                               description="Put the spec on record before any code exists to blur it")) as ph:
        commit(ph, plan)

    with run.phase(PhaseParams(name="test_design", kind="agent", owner="test_designer", retries=1,
                               description="Turn the plan into a suite that fails until the "
                                           "build satisfies it — red is the proof it tests "
                                           "something")) as ph:
        test_design = ph.call(AgentCall(output_type=TestDesignOutput, prompt=prompt,
                                        previous=plan,
                                        gates=[gates.artifacts_exist, gates.tests_red]))

    with run.phase(PhaseParams(name="commit_tests", kind="code", owner="git",
                               description="Land the red suite before the build — the fourth "
                                           "work product gets its own commit and author")) as ph:
        commit(ph, test_design)

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the plan; the red suite in the previous "
                                           "envelope is the finish line")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=test_design,
                                  gates=[gates.diff_matches_claims]))

    # From here the green bar means BOTH suites, always together: the fixed one
    # (existing behavior survived) and the generated one (new behavior arrived).
    both_suites = [test_design.test_file]

    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Run fixed + generated suites — a known command, "
                                               "so code runs it and no agent has to rediscover it")) as ph:
            test = quality.run_tests(run, extra_files=both_suites)
            record(ph, test)

        if test.passed:
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description="Repair what the suite reported, from its "
                                               "verbatim output")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                      previous=quality.as_envelope(test, "tests"),
                                      gates=[gates.diff_matches_claims]))

    review = None
    revised = False
    for i in range(1, MAX_REVISION_LOOPS + 1):
        with run.phase(PhaseParams(name=f"review_{i}", kind="agent", owner="reviewer",
                                   description="Confirm the build matches the plan")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt, previous=build,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent]))

        if review.approved or i == MAX_REVISION_LOOPS:
            break

        with run.phase(PhaseParams(name=f"revise_{i}", kind="agent", owner="builder", retries=1,
                                   description="Close the reviewer's blocking findings")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=review,
                                      gates=[gates.diff_matches_claims]))
            revised = True

    # A revision edited code after the suite last ran, so the green light is
    # stale. Re-run it rather than commit on a result that predates the change.
    if revised and review is not None and review.approved:
        with run.phase(PhaseParams(name="retest", kind="code", owner="quality",
                                   description="Re-run fixed + generated suites — the revision "
                                               "changed code after the last green result")) as ph:
            test = quality.run_tests(run, extra_files=both_suites)
            record(ph, test)

    # Red tests or a rejected review stop the chain here: the code stays
    # uncommitted and nothing is documented. The plan and the red suite commits
    # stand — a spec plus a failing suite is exactly where TDD resumes.
    verified = (test is not None and test.passed
                and review is not None and review.approved)
    if verified:
        with run.phase(PhaseParams(name="commit_build", kind="code", owner="git",
                                   description="Land the code only now: both suites green, approved review")) as ph:
            commit(ph, build)

        with run.phase(PhaseParams(name="changes", kind="code", owner="git",
                                   description="Diff the whole run against its pinned baseline, for the documenter")) as ph:
            changeset = changes.capture(run, ChangeCapture(base=baseline))
            ph.log(base=f"{changeset.base.label} @ {changeset.base.commit[:7]}",
                   reason=changeset.base.reason,
                   files=len(changeset.files) + len(changeset.untracked),
                   lines=f"+{changeset.insertions} -{changeset.deletions}",
                   diff=changeset.diff_path)
            if changeset.empty:
                raise RuntimeError(
                    f"nothing changed since {changeset.base.label} "
                    f"({changeset.base.reason}) — there is nothing to document.")

        with run.phase(PhaseParams(name="document", kind="agent", owner="documenter", retries=1,
                                   description="Write up the completed change")) as ph:
            document = ph.call(AgentCall(output_type=DocumentOutput, prompt=prompt,
                                         previous=changes.as_envelope(changeset, DOCUMENT_NOTES),
                                         gates=[gates.artifacts_exist, gates.files_non_empty]))

        with run.phase(PhaseParams(name="commit_docs", kind="code", owner="git",
                                   description="Ship the write-up in its own commit, beside the code it describes")) as ph:
            commit(ph, document)

    return run.finish(accepted=verified,
                      reason="the suite or the review never came back clean")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
