"""Deterministic lint, typecheck, and build blocks for the payload app.

The app intentionally has no local package toolchain. Linting uses a pinned
Oxlint release through Bun; "typecheck" is the strongest zero-config Bun-native
syntax/import/transpilation check available for the current JS/TS sources.
Build blocks add minification and keep all outputs inside the ADW session.

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
    output_dir = _check_dir(run, "typecheck") / "bundle"
    return _run(QualityCheckSpec(
        name="typecheck",
        area="frontend",
        operation="typecheck",
        argv=[BUN, "build", "--target=browser", ENTRY, "--outdir", str(output_dir)],
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
