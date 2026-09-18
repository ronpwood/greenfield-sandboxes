"""Deterministic lint, typecheck, and build blocks for the payload app.

The app intentionally has no local package toolchain. Linting uses a pinned
Oxlint release through Bun; `typecheck` is real type-checking — a pinned `tsc
--noEmit` over the entry graph, non-strict — so undeclared names and wrong
argument types fail the gate. `build` keeps `bun build`, which is a bundle, not
a check.

That distinction is the whole point of the block. `typecheck` USED to run
`bun build --target=browser`, which strips types without checking them. Measured
2026-09-17 on the gf-e2e-20260917-cbb166 tree: with an undeclared `idx`
reintroduced, `bun build` exits 0 and bundles 18 modules; non-strict tsc reports
`circle-wheel.ts(99,36): error TS2304: Cannot find name 'idx'`. A gate that
compiles a crash is worse than no gate, because the chain reports it as green.

The app's paths come from app.manifest.yaml at the repo root — `just app swap`
edits that file, not this one. What still changes here on a swap is the
command blocks themselves (lint/typecheck/build/test are stack-specific).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityResult,
                         VerifyOutput)
from .manifest import load as load_manifest
from .utils import now_iso, operator_env

OXLINT_VERSION = "1.36.0"

# Pinned for the same reason oxlint is: a gate whose strictness drifts with
# whatever the registry serves today is not a gate, it is a coin flip, and a
# fan-out comparing arms needs every arm checked by the same compiler. Bumping
# this is a DELIBERATE change — expect new errors in code that passed yesterday,
# and make the bump its own commit.
#
# Non-strict on purpose (`--strict false`; tsc 7 defaults to strict). Measured on
# the same tree: strict finds 17 errors, non-strict 15, and the two extra are
# style (possibly-null, implicit-any), not crashes. Fix loops are bounded
# (MAX_FIX_LOOPS = 3) and are better spent on defects than on annotations.
TSC_VERSION = "7.0.2"

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000


# Invoked by bare name, exactly as the operator would type it: an ADW inherits
# their environment, so `bun` resolves off PATH here for the same reason it does
# in their shell. Resolving it to an absolute path instead would hard-code one
# machine's layout into the trace and into the artifact logs.
#
# BUN_PATH remains the escape hatch for an environment that does NOT put bun on
# PATH — a container, or a cron with a stripped PATH. A genuinely missing binary
# is not something to pre-empt: _run's OSError branch already records it as
# exit 127 with the real error text.
BUN = os.environ.get("BUN_PATH", "").strip() or "bun"

# The app is a static, no-backend Bun HTML entry: index.html loads the entry
# as a module, and every other .ts file is a plain import off that graph —
# there is no server.ts, so there is no "backend" area to check. The same
# three names as before, so the command blocks below stay diff-free.
_MANIFEST = load_manifest()
APP_DIR = _MANIFEST.app.dir
ENTRY = _MANIFEST.app.entry
TEST_FILE = _MANIFEST.app.test_file


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(spec: QualityCheckSpec, run) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    command = shlex.join(spec.argv)
    env = operator_env()             # the engineer's own shell environment

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            spec.argv,
            cwd=run.repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or ""
        stderr = (error.stderr or "") + f"\nTimed out after {spec.timeout_seconds}s."
    except OSError as error:
        returncode = 127
        stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    )
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


def lint(run) -> QualityCheckResult:
    return _run(QualityCheckSpec(
        name="lint",
        area="frontend",
        operation="lint",
        argv=[BUN, "x", f"oxlint@{OXLINT_VERSION}", APP_DIR],
    ), run)


def typecheck(run) -> QualityCheckResult:
    """Type-check the entry graph with a pinned tsc.

    ENTRY only, not the whole tree: the test files import `bun:test`, whose types
    need a bun-specific lib this zero-config invocation does not carry. `bun test`
    runs those; tsc checks the code they exercise. Every source file that matters
    is reachable from ENTRY by construction — the app is a single module graph off
    index.html, which is what makes that boundary safe.

    The flags recreate what a bundler assumes, because there is no tsconfig.json
    in the repo to read: `bundler` resolution plus `allowImportingTsExtensions`
    for the `./main.ts` style imports the app uses, and a dom-bearing lib so
    `document` and friends exist. `--skipLibCheck` keeps the cost in this app's
    own code rather than in @types.
    """
    return _run(QualityCheckSpec(
        name="typecheck",
        area="frontend",
        operation="typecheck",
        argv=[BUN, "x", "--package", f"typescript@{TSC_VERSION}", "tsc",
              "--noEmit", "--skipLibCheck", "--strict", "false",
              "--target", "es2022", "--module", "esnext",
              "--moduleResolution", "bundler", "--allowImportingTsExtensions",
              "--lib", "es2022,dom,dom.iterable", ENTRY],
    ), run)


def build(run) -> QualityCheckResult:
    output_dir = _check_dir(run, "build") / "bundle"
    return _run(QualityCheckSpec(
        name="build",
        area="frontend",
        operation="build",
        argv=[BUN, "build", "--target=browser", "--minify", ENTRY, "--outdir", str(output_dir)],
    ), run)


def tests(run, extra_files: list[str] | None = None) -> QualityCheckResult:
    """Run the app's suite. A known command — code, not an agent.

    The command is written down once here because it is not a judgement call.
    An agent rediscovering `bun test` on every run cost ~1M tokens and 85s; this
    costs nothing and takes milliseconds.

    `extra_files` appends more test files to the same command — the TDD chain
    passes its generated suite here so the green bar always means fixed +
    generated together, reported through the one envelope shape every ADW
    already consumes. Zero-arg behavior is byte-identical to before.
    """
    return _run(QualityCheckSpec(
        name="tests",
        area="frontend",
        operation="build",           # the enum has no "test"; the name carries it
        argv=[BUN, "test", TEST_FILE, *(extra_files or [])],
        timeout_seconds=600,
    ), run)


def run_tests(run, extra_files: list[str] | None = None) -> QualityResult:
    """The test suite as a QualityResult, so it reports like every other block."""
    check = tests(run, extra_files)
    failures = ([] if check.passed else
                [f"{check.name}: `{check.command}` exited {check.returncode}\n"
                 f"{check.output_tail}".rstrip()])
    return QualityResult(passed=check.passed, checks=[check], failures=failures,
                         artifacts=[check.output_artifact])


def run_verify(run, extra_files: list[str] | None = None) -> QualityResult:
    """Typecheck, then tests, as one QualityResult.

    This is what the SDLC chains call where they used to call `run_tests`. Order
    matters: a type error is cheaper to read than a runtime failure caused by the
    same mistake, and both are collected either way, so a fix loop gets the whole
    picture in one pass rather than one gate per iteration.

    Both blocks always run — this deliberately does NOT short-circuit on a failed
    typecheck. A builder handed "15 type errors AND these 3 failing tests" can fix
    them together; handed them one gate at a time it spends a bounded fix loop per
    gate. `run_tests` stays as it was for the chains that only want the suite.
    """
    checks = [typecheck(run), tests(run, extra_files)]
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(passed=not failures, checks=checks, failures=failures,
                         artifacts=[check.output_artifact for check in checks])


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so the builder can be handed it directly."""
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Fix every failure below. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )


def run_quality(run) -> QualityResult:
    """Run every quality block, including tests, and collect all failures."""
    blocks: list[Callable] = [lint, typecheck, build, tests]
    checks = [block(run) for block in blocks]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
