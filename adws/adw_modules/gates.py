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
from pathlib import Path

from .data_types import EnvelopeBase, GateReport
from .manifest import load as load_manifest
from .quality import BUN, OXLINT_VERSION

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


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
    """A generated test suite is non-vacuous only if it FAILS on the pre-build tree.

    A green smoke detector only proves it's on; you prove it works by putting
    smoke under it. `verdict_consistent` refutes a reviewer's self-contradiction
    without reading the diff; this refutes a vacuous suite without reading a
    single assertion — "these tests test something" is mechanically checkable
    as "they fail before the build exists".

    Four checks, no hidden state, evidence recorded either way:
      containment — the file is inside app.generated_tests_dir, exists, non-empty
      parses      — oxlint exits 0, so red-by-syntax-error can't masquerade as TDD red
      RED         — `bun test` exits NON-zero; the failure tail rides along as
                    the builder's "make exactly this pass"
      fixed suite — the pre-existing suite is untouched; a designer must not
                    "help" by editing the tests that guard existing behavior

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

        red = _cmd([BUN, "test", test_file])
        tail = (red.stdout + red.stderr)[-TAIL_CHARS:]
        # The tail is evidence on BOTH outcomes: on pass it is what the builder
        # must turn green; on fail it shows the suite already passing (vacuous).
        report.check("RED", red.returncode != 0,
                     f"bun test exit {red.returncode}\n{tail}" if red.returncode != 0
                     else f"suite already passes on the pre-build tree — it has "
                          f"tested nothing (exit 0)\n{tail}")
    else:
        report.check("parses", False, "not run — containment failed")
        report.check("RED", False, "not run — containment failed")

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
