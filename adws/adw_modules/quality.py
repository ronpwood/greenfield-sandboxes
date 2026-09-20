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

import dataclasses
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
    # Ambient declarations are NOT reachable by import, so tsc never sees them
    # when it is handed an entry file explicitly — they have to be named on the
    # command line or they may as well not exist. Without this, an app that does
    # the idiomatic `import "./styles.css"` fails the gate with TS2882 while
    # `bun build` accepts it happily, and a build spends one of its three bounded
    # fix loops on a non-defect. Measured 2026-09-18 on the greenfield shell.
    # node_modules is excluded deliberately: an installed dependency ships
    # hundreds of its own .d.ts files (happy-dom alone contributes ~700), and
    # globbing them onto the command line makes tsc slow and the argv unreadable.
    # Dependency types are already resolved through imports; what has to be named
    # here is only the app's OWN ambient declarations, which nothing imports.
    # Filter on the path RELATIVE to APP_DIR: a `..` in APP_DIR itself starts with
    # a dot and would otherwise trip the hidden-directory check, silently matching
    # nothing at all.
    _app = Path(APP_DIR)
    declarations = sorted(
        str(p) for p in _app.rglob("*.d.ts")
        if not any(part == "node_modules" or part.startswith(".")
                   for part in p.relative_to(_app).parts)
    )
    return _run(QualityCheckSpec(
        name="typecheck",
        area="frontend",
        operation="typecheck",
        argv=[BUN, "x", "--package", f"typescript@{TSC_VERSION}", "tsc",
              "--noEmit", "--skipLibCheck", "--strict", "false",
              "--target", "es2022", "--module", "esnext",
              "--moduleResolution", "bundler", "--allowImportingTsExtensions",
              "--lib", "es2022,dom,dom.iterable", *declarations, ENTRY],
    ), run)


def build(run) -> QualityCheckResult:
    output_dir = _check_dir(run, "build") / "bundle"
    return _run(QualityCheckSpec(
        name="build",
        area="frontend",
        operation="build",
        argv=[BUN, "build", "--target=browser", "--minify", ENTRY, "--outdir", str(output_dir)],
    ), run)


def tests_argv(extra_files: list[str] | None = None) -> list[str]:
    """THE command that grades a build. One definition, so nothing can diverge.

    This exists as a function because a copy of it drifted. `gates.tests_red`
    used to certify a generated suite with `bun test <generated>` while the
    build was graded with `bun test <fixed> <generated>` — a different command,
    and the difference is load-bearing: `bun test` shares one module registry
    across the files on its argv (it isolates only under `--isolate`, which this
    command does not pass). A suite that collides with the fixed one therefore
    passed the gate alone and detonated in `build`, where no agent has the
    authority to fix the cause. Measured 2026-09-20: 1.14M tokens, 34 of 35 tool
    calls, no product code written.

    Any caller that wants to know what the grade will be must call THIS, not
    assemble its own argv.
    """
    return [BUN, "test", TEST_FILE, *(extra_files or [])]


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
        argv=tests_argv(extra_files),
        timeout_seconds=600,
    ), run)


def render(run) -> QualityCheckResult:
    """Load the app in a real browser, drive it, and fail on what only a browser sees.

    Delegated to `adw_modules/render_smoke.py`, which carries the full rationale.
    The short version: `happy-dom` in the fixed suite closed the crash-on-load
    class, but it does no LAYOUT and no HIT-TESTING. Fan-out 3 shipped six arms
    with lint, typecheck and every test green, and two did not work — one had
    eleven of twelve key slices painted over by a single mis-flagged SVG arc, and
    no gate in the chain could see it.

    Exit 2 is deliberately NOT a failure. It means "I could not look at all"
    (no chromium, dev server never came up) — our infrastructure breaking, not
    the builder's code. Spending one of three bounded fix loops on that would be
    strictly worse than not running the check, so it degrades to a pass and says
    so in the log. Only exit 1, a real finding, blocks.
    """
    check = _run(QualityCheckSpec(
        name="render",
        area="frontend",
        operation="build",          # the enum has no "render"; the name carries it
        argv=["uv", "run", str(Path(__file__).with_name("render_smoke.py")), APP_DIR],
        timeout_seconds=300,
    ), run)
    if check.returncode == 2:
        run.console.note("quality render: SKIPPED — could not open a browser "
                         "(infrastructure, not the build); not blocking")
        return dataclasses.replace(check, passed=True)
    return check


def run_tests(run, extra_files: list[str] | None = None) -> QualityResult:
    """The test suite as a QualityResult, so it reports like every other block."""
    check = tests(run, extra_files)
    failures = ([] if check.passed else
                [f"{check.name}: `{check.command}` exited {check.returncode}\n"
                 f"{check.output_tail}".rstrip()])
    return QualityResult(passed=check.passed, checks=[check], failures=failures,
                         artifacts=[check.output_artifact])


def run_verify(run, extra_files: list[str] | None = None) -> QualityResult:
    """Lint, typecheck, tests, then a real browser, as one QualityResult.

    This is what the SDLC chains call where they used to call `run_tests`. Order
    is cheapest-signal-first: lint and type errors are easier to read than a
    runtime failure caused by the same mistake, and all three are collected
    either way, so a fix loop gets the whole picture in one pass rather than one
    gate per iteration.

    All three blocks always run — this deliberately does NOT short-circuit. A
    builder handed "these lint errors AND 15 type errors AND 3 failing tests" can
    fix them together; handed them one gate at a time it spends a bounded fix
    loop per gate. `run_tests` stays as it was for the chains that only want the
    suite.

    `lint` runs oxlint's DEFAULT rules, which every tree passes today — the
    greenfield shell, the default payload app, and all four 2026-09-18 arms. It is
    here for the ordinary lint errors the TDD chain never checked, not as a crash
    gate.

    It deliberately does NOT enable `typescript/no-explicit-any`, though that was
    the original reason for adding lint: both 2026-09-18 browser crashes hid
    behind an explicit `any` (`h(): any`, `Map<string, any>`), which defeats tsc
    in strict and non-strict mode identically. Measured before deciding — the
    rule WOULD have flagged both crashed arms (55 and 5 errors), but it also
    flags the arm that worked and shipped (6) and the default payload app (2).
    A gate that fails working code to catch a hazard that `happy-dom` now
    catches directly is a bad trade, so it stays off. Revisit only if a future
    crash slips past the fixed suite.

    `render` runs LAST because it is by far the most expensive block (it boots a
    dev server and a chromium) and because everything ahead of it is a cheaper
    read on the same mistake. It is the answer to fan-out 3's central result: all
    six arms passed lint, typecheck and every test, and two of them did not work.
    Both remaining defect classes were render-only, so no amount of source-level
    checking could reach them — see `render_smoke.py` for what it does and does
    NOT catch, which is stated honestly there rather than overclaimed here.
    """
    checks = [lint(run), typecheck(run), tests(run, extra_files), render(run)]
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
