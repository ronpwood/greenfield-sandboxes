"""Offline check of the suite-change capture the reviewer is handed.

    uv run --with pydantic --with pyyaml python -m adws.adw_modules.suite_changes_selftest

Builds a throwaway git repo, commits a fixed suite and a red suite as "red",
then makes the four kinds of edit the 2026-10-02 audit found or feared: growth
(appended tests), a value correction (mtg1 V67's shape: toBe(930) -> toBe(1320)),
a weakening (a deleted expect), and a new untracked suite file. Asserts the
counts, that every removed line reaches the note verbatim, the zero case, and
truncation. Needs no VM, no model, no network. Exit 1 on any failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from . import changes

FIXED = "apps/a/a.test.ts"
RED = "apps/a/tests/generated/r.test.ts"
PATHS = [FIXED, "apps/a/tests/generated"]

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not ok:
        failures.append(f"  {name}" + (f": {detail}" if detail else ""))


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def repo(files: dict[str, str]) -> tuple[str, SimpleNamespace]:
    """A fresh repo holding `files`, committed as red. Returns (red sha, run stub)."""
    root = Path(tempfile.mkdtemp(prefix="suite-selftest-"))
    os.chdir(root)
    git("init", "-q")
    git("config", "user.email", "selftest@local")
    git("config", "user.name", "selftest")
    for path, text in files.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)
    git("add", "-A")
    git("commit", "-q", "-m", "red")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    handoff = root / "handoff"
    handoff.mkdir()
    return sha, SimpleNamespace(context_handoff_dir=handoff)


def edit(path: str, old: str, new: str) -> None:
    text = Path(path).read_text()
    assert old in text, (path, old)
    Path(path).write_text(text.replace(old, new))


def main() -> int:
    here = os.getcwd()
    try:
        # ── the four edits ───────────────────────────────────────────────────
        sha, run = repo({
            FIXED: "test('a', () => { expect(1).toBe(1); });\n",
            RED: "test('v67', () => {\n  expect(x).toBe(930);\n  expect(y).toBe(2);\n});\n-- sql-ish\n",
        })
        with open(FIXED, "a") as f:
            f.write("test('b', () => {\n  expect(2).toBe(2);\n});\n")       # growth: +3
        edit(RED, "expect(x).toBe(930);", "expect(x).toBe(1320);")          # correction: +1 -1
        edit(RED, "  expect(y).toBe(2);\n", "")                             # weakening: -1
        edit(RED, "-- sql-ish\n", "")                                      # "--- " inside a hunk: -1
        Path("apps/a/tests/generated/new.test.ts").write_text("test('n', () => {});\n")

        sc = changes.capture_suites(run, sha, PATHS)
        check("added == 4", sc.added == 4, f"got {sc.added}")
        check("removed == 3", sc.removed == 3, f"got {sc.removed}")
        check("files", sorted(sc.files) == sorted([FIXED, RED]), f"got {sc.files}")
        check("untracked", sc.untracked == ["apps/a/tests/generated/new.test.ts"], f"got {sc.untracked}")
        joined = "\n".join(sc.removed_lines)
        check("removed 930", "toBe(930)" in joined and RED in joined, joined)
        check("removed expect(y)", "expect(y).toBe(2);" in joined, joined)
        check("a removed '-- x' line is content, not a header",
              f"{RED} " in joined and "-- sql-ish" in joined and len(sc.removed_lines) == 3, joined)
        check("diff file", Path(sc.diff_path).is_file()
              and "toBe(930)" in Path(sc.diff_path).read_text()
              and "expect(2).toBe(2)" in Path(sc.diff_path).read_text(), sc.diff_path)

        team = changes.suite_notes(sc, amendments=True)
        tdd = changes.suite_notes(sc, amendments=False)
        for needle in ("toBe(930)", "expect(y).toBe(2);", "+4", "-3", "new.test.ts", sc.diff_path):
            check(f"team note has {needle!r}", needle in team, team)
        check("team note has amendment rule", "accepted amendment" in team, team)
        check("tdd note omits amendment rule", "amendment" not in tdd, tdd)
        check("both carry the blocking rule", "blocking" in team and "blocking" in tdd)

        # ── zero case: growth only ───────────────────────────────────────────
        sha, run = repo({FIXED: "test('a', () => {});\n"})
        with open(FIXED, "a") as f:
            f.write("test('b', () => {});\n")
        sc = changes.capture_suites(run, sha, PATHS)
        note = changes.suite_notes(sc, amendments=True)
        check("zero: removed == 0", sc.removed == 0, f"got {sc.removed}")
        check("zero: one line", len(note.splitlines()) == 1, repr(note))
        check("zero: says nothing removed", "nothing removed" in note, note)

        # ── truncation: 70 removed lines ─────────────────────────────────────
        body = "".join(f"  expect(v{i}).toBe({i});\n" for i in range(70))
        sha, run = repo({RED: "test('many', () => {\n" + body + "});\n"})
        edit(RED, body, "")
        sc = changes.capture_suites(run, sha, PATHS)
        note = changes.suite_notes(sc, amendments=False)
        shown = sum(1 for line in note.splitlines() if "expect(v" in line)
        check("truncation: removed == 70", sc.removed == 70, f"got {sc.removed}")
        check("truncation: 60 shown", shown == changes.MAX_NOTE_LINES == 60, f"shown {shown}")
        check("truncation: points at the diff", "10 more" in note and sc.diff_path in note, note)
    finally:
        os.chdir(here)

    if failures:
        print(f"suite_changes selftest: {len(failures)} of {checks} checks FAILED")
        print("\n".join(failures))
        return 1
    print(f"suite_changes selftest: {checks} checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
