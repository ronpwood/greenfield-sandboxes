"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import quality
from .data_types import EnvelopeBase, GateReport
from .manifest import load as load_manifest
from .quality import BUN, OXLINT_VERSION

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def _junit_tally(path: Path) -> dict[str, tuple[int, int]]:
    """Per-FILE (total, failed) from a bun junit report — attribution an exit code cannot give.

    Keyed on each testcase's own `file` attribute, which bun writes exactly as
    the path appeared on the argv. A file that failed to load has no testcases
    and so no key at all: absence is the signal, not a zero.
    """
    tally: dict[str, tuple[int, int]] = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return tally                 # caller reads this as "reported nothing"
    for case in root.iter("testcase"):
        key = case.get("file") or ""
        total, failed = tally.get(key, (0, 0))
        tally[key] = (total + 1, failed + (1 if case.find("failure") is not None else 0))
    return tally


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must exist on disk, and at least one must be claimed.

    A builder that declares zero changed_files would otherwise pass every
    per-file check vacuously (0 checked, 0 failed) — the emptiness itself is
    the violation, so it gets its own check instead of a silent green.
    """
    report = GateReport()
    changed = getattr(envelope, "changed_files", [])
    report.check("changed_files declared", bool(changed),
                 f"{len(changed)} file(s) claimed" if changed
                 else "no changed_files declared — the build claims nothing changed")
    for f in changed:
        p = Path(f)
        report.check(f, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "claimed changed file does not exist")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def tests_red(envelope: EnvelopeBase, run) -> GateReport:
    """A generated suite is non-vacuous only if it FAILS — under the GRADING command.

    A green smoke detector only proves it's on; you prove it works by putting
    smoke under it. `verdict_consistent` refutes a reviewer's self-contradiction
    without reading the diff; this refutes a vacuous suite without reading a
    single assertion — "these tests test something" is mechanically checkable
    as "they fail before the build exists".

    The command matters as much as the verdict. This gate used to run `bun test
    <generated>` while `build` was graded by `quality.tests()` running `bun test
    <fixed> <generated>`. `bun test` shares one module registry across the files
    on its argv, so a generated suite can pass here alone and, run alongside the
    fixed suite, throw at module load before a single test executes. That is a
    green gate handing the builder an unbuildable tree, and the builder cannot
    fix the cause — the fixed suite is not its file to edit. So the command here
    IS `quality.tests_argv()`, by call and not by copy.

    Six checks, no hidden state, evidence recorded either way:
      containment  — the file is inside app.generated_tests_dir, exists, non-empty
      parses       — oxlint exits 0, so red-by-syntax-error can't masquerade as TDD red
      runs         — under the grading command the generated file produces test
                     results at all; a file that dies at module load produces
                     none, and an exit code alone cannot tell that from red
      RED          — at least one GENERATED test fails; the failure tail rides
                     along as the builder's "make exactly this pass"
      fixed suite green — the fixed suite still passes in that same process, so
                     the generated file has not poisoned the guard on existing
                     behavior
      fixed suite untouched — the pre-existing suite is unedited; a designer must
                     not "help" by editing the tests that guard existing behavior

    What `parses` deliberately does NOT do: resolve imports. A TDD test
    legitimately imports a module that doesn't exist yet; `bun build` would
    reject that, oxlint doesn't try.
    """
    manifest = load_manifest()
    gen_dir = manifest.app.generated_tests_dir.rstrip("/") + "/"
    report = GateReport()

    test_file = str(getattr(envelope, "test_file", "") or "")
    path = Path(run.repo_root) / test_file if test_file else None
    contained = (test_file.startswith(gen_dir)
                 and path is not None and path.is_file() and path.stat().st_size > 0)
    report.check("containment", contained,
                 f"{test_file} inside {gen_dir}, {_size(path)}" if contained
                 else (f"test_file {test_file!r} is not a non-empty file under {gen_dir}"
                       if test_file else "envelope declares no test_file"))

    def _cmd(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=run.repo_root, capture_output=True, text=True)

    if contained:
        lint = _cmd([BUN, "x", f"oxlint@{OXLINT_VERSION}", test_file])
        report.check("parses", lint.returncode == 0,
                     f"oxlint exit {lint.returncode}" + (
                         "" if lint.returncode == 0
                         else " — red must come from assertions or missing imports, "
                              "never a file that cannot parse\n"
                              + (lint.stdout + lint.stderr)[-TAIL_CHARS:]))

        # THE grading command, by call and not by copy — see quality.tests_argv.
        fixed_file = manifest.app.test_file
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "red.xml"
            argv = quality.tests_argv([test_file]) + [
                "--reporter=junit", f"--reporter-outfile={junit}"]
            red = _cmd(argv)
            tally = _junit_tally(junit)
        command = " ".join(argv[:-2])
        tail = (red.stdout + red.stderr)[-TAIL_CHARS:]

        gen_total, gen_failed = tally.get(test_file, (0, 0))
        fix_total, fix_failed = tally.get(fixed_file, (0, 0))

        # A file that throws at module load reports NOTHING, and the process
        # still exits non-zero — indistinguishable from red by exit code alone.
        # That is the collision this check exists to name, at test_design time,
        # while the designer still holds the pen on the file that causes it.
        report.check("runs", gen_total > 0,
                     f"{gen_total} test(s) reported under `{command}`" if gen_total
                     else f"{test_file} produced NO test results under `{command}` — it "
                          f"runs alone but not alongside {fixed_file}. `bun test` shares "
                          f"one module registry across the files on its argv (no "
                          f"--isolate), so a second happy-dom install, a duplicate "
                          f"global, or a cached `import` of the entry dies here before "
                          f"a single test executes. Fix it in this file — the builder "
                          f"cannot, {fixed_file} is not its to edit.\n{tail}")

        # The tail is evidence on BOTH outcomes: on red it is what the builder
        # must turn green; on green it shows the suite passing already (vacuous).
        report.check("RED", gen_failed > 0,
                     f"{gen_failed}/{gen_total} generated test(s) fail on the "
                     f"pre-build tree\n{tail}" if gen_failed
                     else (f"all {gen_total} generated test(s) already pass — the suite "
                           f"has tested nothing\n{tail}" if gen_total
                           else "not assessable — the suite reported no results"))

        report.check("fixed suite green", fix_total > 0 and fix_failed == 0,
                     f"{fix_total} test(s) in {fixed_file} still pass alongside it"
                     if fix_total and not fix_failed
                     else (f"{fix_failed}/{fix_total} test(s) in {fixed_file} FAIL when "
                           f"run alongside {test_file} — the generated suite has "
                           f"poisoned the guard on existing behavior\n{tail}"
                           if fix_total
                           else f"{fixed_file} reported no results under `{command}`"
                                f"\n{tail}"))
    else:
        for name in ("parses", "runs", "RED", "fixed suite green"):
            report.check(name, False, "not run — containment failed")

    diff = _cmd(["git", "diff", "--name-only", "--", manifest.app.test_file])
    dirty = diff.stdout.strip()
    report.check("fixed suite untouched", not dirty,
                 f"{manifest.app.test_file} clean" if not dirty
                 else f"the fixed suite was edited: {dirty}")
    return report


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
