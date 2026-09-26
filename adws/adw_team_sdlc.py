#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Team SDLC — the TDD chain, run by a team that shares one living spec.

Usage:
    uv run adws/adw_team_sdlc.py "<prompt or path/to/prompt.md>" --config adws/adw_sssf_config/sssf.team.config.yaml [--adw-id a1b2c3d4]

Phases: engineer(request) -> planner [gates: artifacts_exist, files_non_empty, spec_form]
        -> git(commit_plan)
        -> test_designer [+ spec_frozen] -> git(commit_tests)
        -> builder [+ spec_frozen, build_claims] -> code(verify) [-> builder(fix) ... bounded]
        -> reviewer [+ spec_frozen, amendments_ruled, values_swept] [-> builder(revise) ... bounded]
        -> code(retest, only if a revision changed code)
        -> code(sync_spec) -> git(commit_build) -> code(changes) -> documenter -> git(commit_docs)

WHY IT EXISTS. The prompts in the TDD chain are about getting out: implement
the plan exactly, emit the Report JSON, you are graded by one command. The
planner is asked for files and changes, never for what right looks like, so
the why is gone after the first handoff and the reviewer approves whatever the
plan said. Reachable value defects shipped in 2 of 4 valid runs that way
(CHANGELOG 2026-09-24f). This chain treats the agents as a team that owns the
outcome together, with specs/team-ownership-prompts.md as its design.

WHAT DIFFERS FROM adw_tdd_sdlc.py, and nothing else does:
  - The spec, <context_handoff_dir>/plan.md, has a fixed eight-section form
    (adw_modules/team_spec.py). `spec_form` checks the planner wrote it, with an
    Expected values table whose every row carries a first-principles derivation.
  - Three sections are frozen once `commit_plan` lands. Every later agent may
    annotate the spec, but changes a requirement or an expected value only by
    proposing an amendment; `spec_frozen` refutes an in-place edit, and the
    reviewer rules on every amendment (`amendments_ruled`).
  - Every review sweeps every effective V against the delivered code and says
    what it saw (`values_swept`) — the enumerated check that caught harn5's bug
    and whose absence let harn7's ship.
  - `sync_spec` copies the annotated spec over its committed copy, so the diff
    of that file across the run is the team's record, brief to delivered app.
  - Agent phases that carry a spec gate get one retry, so a violation goes back
    to the agent that made it as a correction instead of ending the run.

adw_tdd_sdlc.py is this chain's control and stays untouched: same roster
models, old prompts, no spec gates. The roster that goes with this chain is
sssf.team.config.yaml; this chain on the default roster would fail `spec_form`,
because the default planner is never asked for the form.
"""

import argparse
import sys

import shutil
import subprocess
from pathlib import Path

from adw_modules import agents, changes, gates, git_helper, quality, session, utils
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    DocumentOutput, PhaseParams, PlanOutput,
                                    ReviewOutput, TestDesignOutput)

REQUIRED_AGENTS = ["planner", "test_designer", "builder", "reviewer", "documenter"]
MAX_FIX_LOOPS = 3

# Revisions the BUILDER actually gets. The loop runs one review more than this,
# because it must END on a verdict: a revision with no review after it would
# ship unexamined code. So reviews = MAX_REVISIONS + 1, and the final review's
# findings are, by construction, never acted on.
#
# That last sentence used to be a silent bug. The constant was `MAX_REVISION_LOOPS
# = 2` and it read like a revision budget, but it bounded REVIEWS: every run got
# review_1 -> revise_1 -> review_2 -> stop, i.e. exactly ONE revision, and
# review_2's findings were discarded in every run this repo has ever done.
# Measured on 2026-09-18: gf3-3 went 11 blocking findings -> 8 (18 of 24
# requirements met) and was cut off there; gf3-1 closed 8 of 10 and its reviewer
# called the two survivors "small, localized". Both were one loop from done.
#
# Now the name means what it says, and the budget is 2 revisions (3 reviews).
MAX_REVISIONS = 2
MAX_REVIEWS = MAX_REVISIONS + 1

# Goes to the LAST review only. gf3-6 was approved on a review scoped to a
# single-file fix, so nothing ever audited the app it shipped — a wheel whose
# controls were never wired. A final verdict has to be about the delivered
# thing, not about the most recent diff.
FINAL_REVIEW_NOTES = (
    "This is the LAST review of this run: no revision follows it, so your verdict is final "
    "and whatever you approve is what the person who asked receives. Audit the DELIVERED APP "
    "as a whole, not just the most recent diff. Every requirement in the effective spec must be "
    "present AND work end to end — a control that exists but is wired to nothing is a blocking "
    "finding, not a nitpick. Re-run your value sweep over every V against the code as it is "
    "now, and rule on every amendment still marked proposed."
)

DOCUMENT_NOTES = ("Read diff_path in full before writing. Document only what the "
                  "diff shows, then copy the write-up into app_docs/ as your task "
                  "describes.")


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.team.config.yaml", adw_id: str | None = None) -> int:
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

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner", retries=1,
                               description="Write the team's spec: what we are solving for, "
                                           "the requirements, and the expected values each "
                                           "derived from first principles")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty,
                                        gates.spec_form]))

    with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                               description="Put the spec on record before any code exists to blur it")) as ph:
        # The frozen sections are measured against THIS commit for the rest of
        # the run. The planner declares two artifacts: the handoff plan.md and
        # its repo copy under specs/ — the second is the committed record.
        handoff = Path(run.context_handoff_dir).resolve()
        committed = [a for a in plan.artifacts
                     if handoff not in Path(a).resolve().parents]
        if len(committed) != 1:
            raise RuntimeError(f"expected exactly one repo copy of the spec in the plan's "
                               f"artifacts, got {committed!r}")
        committed_spec = committed[0]
        # Re-copy before committing: a spec_form correction edits plan.md after
        # the planner's own copy, and a stale copy would make spec_frozen blame
        # the NEXT agent for the planner's fix.
        shutil.copyfile(handoff / "plan.md", Path(run.repo_root) / committed_spec)
        commit(ph, plan)
        plan_sha = git_helper.rev("HEAD")
        ph.log(spec=committed_spec, frozen_at=git_helper.short_sha(plan_sha))
    frozen = gates.spec_frozen(committed_spec, plan_sha)

    with run.phase(PhaseParams(name="test_design", kind="agent", owner="test_designer", retries=1,
                               description="Turn the plan into a suite that fails until the "
                                           "build satisfies it — red is the proof it tests "
                                           "something")) as ph:
        test_design = ph.call(AgentCall(output_type=TestDesignOutput, prompt=prompt,
                                        previous=plan,
                                        gates=[gates.artifacts_exist, gates.tests_red, frozen]))

    with run.phase(PhaseParams(name="commit_tests", kind="code", owner="git",
                               description="Land the red suite before the build — the fourth "
                                           "work product gets its own commit and author")) as ph:
        commit(ph, test_design)
        # The count the builder's growth is measured against, once, on the
        # committed red tree: nothing the builder does can move it.
        durable_baseline = gates.durable_suite_count(run.repo_root)
        ph.log(durable_tests=durable_baseline)
    growth = gates.durable_suite_growth(durable_baseline)

    builder_gates = [gates.diff_matches_claims, growth, frozen, gates.build_claims]

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder", retries=1,
                               description="Make the spec true for the person who asked; the "
                                           "red suite is the floor, the expected values the "
                                           "standard")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=test_design,
                                  gates=builder_gates))

    # From here the green bar means BOTH suites, always together: the fixed one
    # (existing behavior survived) and the generated one (new behavior arrived).
    both_suites = [test_design.test_file]

    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Typecheck, then run fixed + generated suites — "
                                               "known commands, so code runs them and no agent "
                                               "has to rediscover them")) as ph:
            test = quality.run_verify(run, extra_files=both_suites)
            record(ph, test)

        if test.passed:
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description="Repair what the suite reported, from its "
                                               "verbatim output")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                      previous=quality.as_envelope(test, "tests"),
                                      gates=builder_gates))

    review = None
    revised = False
    for i in range(1, MAX_REVIEWS + 1):
        final = i == MAX_REVIEWS
        with run.phase(PhaseParams(name=f"review_{i}", kind="agent", owner="reviewer", retries=1,
                                   description="Judge the delivered code against the effective "
                                               "spec, sweep every expected value, rule on every "
                                               "amendment")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt,
                                       previous=agents.with_notes(build, FINAL_REVIEW_NOTES) if final else build,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent,
                                              frozen, gates.amendments_ruled, gates.values_swept]))

        if review.approved or final:
            break

        with run.phase(PhaseParams(name=f"revise_{i}", kind="agent", owner="builder", retries=1,
                                   description="Close the reviewer's blocking findings")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=review,
                                      gates=builder_gates))
            revised = True

    # A revision edited code after the suite last ran, so the green light is
    # stale. Re-run it rather than commit on a result that predates the change.
    if revised and review is not None and review.approved:
        with run.phase(PhaseParams(name="retest", kind="code", owner="quality",
                                   description="Typecheck, then re-run fixed + generated suites "
                                               "— the revision changed code after the last "
                                               "green result")) as ph:
            test = quality.run_verify(run, extra_files=both_suites)
            record(ph, test)

    # Always, verified or not: the annotated spec is the team's record either
    # way. Copied, not committed — commit_build picks it up on an approved run,
    # snapshot_run_branch.sh on any other.
    with run.phase(PhaseParams(name="sync_spec", kind="code", owner="git",
                               description="Copy the team's annotated spec over its committed "
                                           "copy, so the diff of that file is the run's record")) as ph:
        shutil.copyfile(Path(run.context_handoff_dir) / "plan.md", Path(run.repo_root) / committed_spec)
        stat = subprocess.run(["git", "diff", "--stat", "--", committed_spec], cwd=run.repo_root,
                              capture_output=True, text=True).stdout.strip()
        ph.log(spec=committed_spec, stat=stat or "unchanged")

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
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.team.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
