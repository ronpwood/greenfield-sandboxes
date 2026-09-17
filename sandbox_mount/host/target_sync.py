#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Regenerate a named target's factory from this repo's HEAD.

A target (targets/<name>.yaml) is a separate clean-room repo a sandbox can mount
instead of this one: same factory, different payload, fresh history. It exists so
no arm can reach this repo's reference app through `git log`, archive/, specs/ or
app_docs/. That guarantee only holds while the factory copy stays leak-free, and
the copy only stays current if regenerating it is one command. This is that command.

    uv run sandbox_mount/host/target_sync.py <name> [--dry-run] [--push]
    just target sync <name> [--dry-run] [--push]

Steps, stopping at the first failure:
  1. preconditions  checkout exists, is clean, on target.branch, fast-forwards to origin;
                    its app.manifest.yaml app: equals the target file's (the contract);
                    pristine guard: pristine_paths equal target.pristine and no tracked path
                    lies outside sync_paths/owned (a merged sandbox run fails here)
  2. export         `git archive HEAD -- <sync_paths>` — tracked files only, so untracked
                    run debris under adws/adw_data/sessions/ can never ship
  3. exclusions     drop just/<host app>.just and the justfile's `mod <host app>` line,
                    both derived from the host manifest
  4. leak check     over the export only (owned files legitimately describe the target's
                    app): host app.name + archive/ app names + target.leak_patterns
  5. mirror         each sync path made identical to the export — adds, changes and
                    deletes — refusing any write under an `owned` path
  6. gates          inside the checkout: manifest read, bun build, bun test, roster validate
  7. commit         neutral message, `factory sync <UTC date>` — never this repo's name
  8. push           --push only (outward-facing, public repo)
  9. provenance     .sandbox/targets/<name>.json {synced_at, host_sha, target_sha, pushed}

--dry-run runs 1-6, prints the diff stat, and resets the checkout.
--check-pristine runs only the pristine guard against the checkout as it is (read-only).

The leak check derives every forbidden name; none is written here. This file is
itself under a sync path, so a literal app name in it would fail its own scan.

Exit codes: 0 synced or nothing to do · 1 precondition/config error · 2 leak found
            3 gate failed · 4 push failed (local commit kept, pushed: false)
            5 target not pristine (a sandbox run's work is on the branch; nothing touched)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# sandbox_mount/host/target_sync.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "adws" / "adw_modules"))
import manifest  # noqa: E402 — the one reader of app.manifest.yaml and targets/*.yaml

PROVENANCE_DIR = REPO_ROOT / ".sandbox" / "targets"
ARCHIVE_SUFFIX = re.compile(r"-\d{8}-\d{6}$")  # just app archive's timestamp


class SyncError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def log(msg: str) -> None:
    print(msg, flush=True)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check)


def under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


# ── config ───────────────────────────────────────────────────────────────────

def load_config(name: str) -> dict:
    if name == manifest.DEFAULT_TARGET:
        raise SyncError(1, "the default target is this repo; there is nothing to sync")
    try:
        target = manifest.target_section(name)
        app = manifest.load_target(name).app
    except (ValueError, FileNotFoundError) as e:
        raise SyncError(1, str(e)) from None

    for key in ("checkout", "branch", "sync_paths", "owned"):
        if not target.get(key):
            raise SyncError(1, f"targets/{name}.yaml target.{key} is required")
    for key in ("sync_paths", "owned"):
        for p in target[key]:
            if p.startswith("/") or ".." in Path(p).parts:
                raise SyncError(1, f"target.{key} entry {p!r} must be a relative path inside the repo")
    pristine = target.get("pristine")
    pristine_paths = [str(p).rstrip("/") for p in target.get("pristine_paths") or []]
    if pristine_paths and not (isinstance(pristine, str) and re.fullmatch(r"[0-9a-f]{40}", pristine)):
        raise SyncError(1, f"targets/{name}.yaml target.pristine must be a full 40-hex sha when pristine_paths is set")
    for p in pristine_paths:
        # A synced path is regenerated on every sync, so guarding it would only ever
        # fire on factory drift. The guard is about what the target owns.
        if any(under(p, sp) or under(sp, p) for sp in target["sync_paths"]):
            raise SyncError(1, f"target.pristine_paths entry {p!r} overlaps a sync path")
    overlaps = [(s, o) for s in target["sync_paths"] for o in target["owned"] if under(s, o) or under(o, s)]
    if overlaps:
        raise SyncError(1, "sync_paths overlap owned paths: " + ", ".join(f"{s} ~ {o}" for s, o in overlaps))

    return {
        "name": name,
        "app": app,
        "checkout": (REPO_ROOT / target["checkout"]).resolve(),
        "branch": str(target["branch"]),
        "sync_paths": [str(p).rstrip("/") for p in target["sync_paths"]],
        "owned": [str(p).rstrip("/") for p in target["owned"]],
        "leak_patterns": [str(p) for p in target.get("leak_patterns") or []],
        "pristine": pristine,
        "pristine_paths": pristine_paths,
    }


# ── 1. preconditions ─────────────────────────────────────────────────────────

def preconditions(cfg: dict) -> None:
    co, branch = cfg["checkout"], cfg["branch"]
    if not (co / ".git").exists():
        raise SyncError(1, f"target checkout {co} is not a git repo")
    if git(co, "status", "--porcelain").stdout.strip():
        raise SyncError(1, f"target checkout {co} is dirty — commit or stash owned edits first")
    current = git(co, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
    if current != branch:
        raise SyncError(1, f"target checkout is on {current or 'a detached HEAD'!r}, not {branch!r}")
    if git(co, "fetch", "--quiet", "origin", branch, check=False).returncode != 0:
        raise SyncError(1, f"could not fetch origin/{branch} in {co}")
    remote = f"origin/{branch}"
    if git(co, "merge-base", "--is-ancestor", remote, "HEAD", check=False).returncode == 0:
        pass  # up to date, or ahead with an earlier unpushed sync
    elif git(co, "merge", "--ff-only", "--quiet", remote, check=False).returncode != 0:
        raise SyncError(1, f"{co} has diverged from {remote} — reconcile by hand")
    else:
        log(f"   fast-forwarded checkout to {remote}")

    if git(REPO_ROOT, "status", "--porcelain").stdout.strip():
        log("   WARN: host tree is dirty — syncing from HEAD, uncommitted edits are NOT included")

    try:
        own = manifest.load(co).app
    except (ValueError, FileNotFoundError) as e:
        raise SyncError(1, f"checkout manifest unreadable: {e}") from None
    if own != cfg["app"]:
        raise SyncError(1, f"contract mismatch: {co}/app.manifest.yaml app: != targets/{cfg['name']}.yaml app:\n"
                           f"   checkout: {own.model_dump()}\n   target:   {cfg['app'].model_dump()}")


def pristine_guard(cfg: dict) -> str:
    """Refuse a target whose branch carries a sandbox run's work. Read-only.

    Harvest keeps runs in local refs/sandbox/*, so the only way a run reaches the
    remote is someone merging it into the branch and the next --push publishing
    it — after which every arm clones an earlier arm's answer. Content, not
    ancestry: a squash, cherry-pick or hand copy has no common commit to find.
    Two checks, because each misses a case alone: a plan-only merge leaves apps/
    identical but adds specs/; a hand-copied app adds no stray path.
    """
    co, pristine, paths = cfg["checkout"], cfg["pristine"], cfg["pristine_paths"]
    if not paths:
        return "no pristine_paths declared — guard off"
    if git(co, "cat-file", "-e", f"{pristine}^{{commit}}", check=False).returncode != 0:
        raise SyncError(1, f"target.pristine {pristine[:12]} is not a commit in {co}")

    details, summary = [], []
    if git(co, "diff", "--quiet", pristine, "HEAD", "--", *paths, check=False).returncode != 0:
        changed = git(co, "diff", "--name-only", pristine, "HEAD", "--", *paths).stdout.split()
        stat = git(co, "diff", "--stat", pristine, "HEAD", "--", *paths).stdout.rstrip()
        summary.append(f"{len(changed)} file(s) under {', '.join(paths)} differ")
        details.append(f"differs from pristine {pristine[:12]}:\n{stat}")
    allowed = cfg["sync_paths"] + cfg["owned"]
    tracked = git(co, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
    stray = [t for t in tracked if not any(under(t, a) for a in allowed)]
    if stray:
        summary.append(f"{len(stray)} stray path(s)")
        details.append("tracked outside sync_paths/owned:\n" + "\n".join(f"   {t}" for t in stray))
    if summary:
        branch, name = cfg["branch"], cfg["name"]
        # First line is the one-line summary `just target show` reports.
        raise SyncError(5, "target not pristine — " + " / ".join(summary) + "\n"
                           f"a sandbox run appears merged into {branch}.\n" + "\n".join(details) + "\n"
                           "Keep results in their own repo (PLAYBOOK § Greenfield runs → Keeping a result). "
                           f"To recover, reset {branch} to the last factory sync. "
                           f"If the shell change is intended, bump target.pristine in targets/{name}.yaml.")
    return f"ok ({', '.join(paths)} unchanged since {pristine[:12]})"


# ── 2-3. export + derived exclusions ─────────────────────────────────────────

def export(cfg: dict, dest: Path, host_app: str) -> dict[str, tarfile.TarInfo]:
    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", "--format=tar", "HEAD", "--", *cfg["sync_paths"]],
                             capture_output=True, check=False)
    if archive.returncode != 0:
        raise SyncError(1, f"git archive failed: {archive.stderr.decode().strip()}")
    members: dict[str, tarfile.TarInfo] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        for m in tar.getmembers():
            if m.isdir():
                continue
            if not (m.isfile() or m.issym()):
                raise SyncError(1, f"unsupported entry type in export: {m.name}")
            members[m.name] = m
        tar.extractall(dest, filter="tar")

    # The host's own app module is payload, not factory.
    app_module = f"just/{host_app}.just"
    if app_module in members:
        (dest / app_module).unlink()
        del members[app_module]
        log(f"   excluded {app_module}")
    if "justfile" in members:
        lines = (dest / "justfile").read_text().splitlines(keepends=True)
        mod = re.compile(rf"^\s*mod\s+{re.escape(host_app)}\b")
        out: list[str] = []
        i = 0
        while i < len(lines):
            if mod.match(lines[i]):
                log(f"   stripped justfile: {lines[i].strip()}")
                while out and out[-1].lstrip().startswith("#"):
                    out.pop()  # the comment that introduces the line goes with it
                if i + 1 < len(lines) and not lines[i + 1].strip():
                    i += 1  # and the blank line that separates it from the next block
            else:
                out.append(lines[i])
            i += 1
        (dest / "justfile").write_text("".join(out))
    return members


# ── 4. leak check ────────────────────────────────────────────────────────────

def forbidden_patterns(cfg: dict, host_app: str) -> list[str]:
    names = {host_app}
    archive = REPO_ROOT / "archive"
    if archive.is_dir():
        names |= {ARCHIVE_SUFFIX.sub("", d.name) for d in archive.iterdir() if d.is_dir()}
    names |= set(cfg["leak_patterns"])
    return sorted({n.lower() for n in names if n.strip()})


def leak_scan(root: Path, files: list[str], patterns: list[str]) -> list[str]:
    rx = re.compile("|".join(re.escape(p) for p in patterns), re.IGNORECASE)
    hits = []
    for rel in sorted(files):
        path = root / rel
        if path.is_symlink():
            text = os.readlink(path)
            lines = [text]
        else:
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                continue  # binary
            lines = data.decode("utf-8", errors="replace").splitlines()
        for n, line in enumerate(lines, 1):
            for m in rx.finditer(line):
                hits.append(f"{rel}:{n}: {m.group(0).lower()}")
    # A path name can leak too (e.g. a file named after the app).
    for rel in sorted(files):
        if rx.search(rel):
            hits.append(f"{rel}:0: (path name)")
    return hits


# ── 5. mirror ────────────────────────────────────────────────────────────────

def mirror(cfg: dict, src: Path, exported: dict[str, tarfile.TarInfo]) -> dict[str, list[str]]:
    co = cfg["checkout"]

    def guard(rel: str) -> None:
        for o in cfg["owned"]:
            if under(rel, o):
                raise SyncError(1, f"refusing to write owned path {rel} (owned: {o})")

    existing = set(git(co, "ls-files", "-z", "--", *cfg["sync_paths"]).stdout.split("\0")) - {""}
    changes: dict[str, list[str]] = {"added": [], "changed": [], "deleted": []}

    for rel in sorted(existing - exported.keys()):
        guard(rel)
        (co / rel).unlink(missing_ok=True)
        changes["deleted"].append(rel)
        parent = (co / rel).parent
        while parent != co and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    for rel in sorted(exported):
        guard(rel)
        s, d = src / rel, co / rel
        if d.is_symlink() or s.is_symlink():
            same = d.is_symlink() and s.is_symlink() and os.readlink(d) == os.readlink(s)
        else:
            same = d.is_file() and d.read_bytes() == s.read_bytes() \
                   and (d.stat().st_mode & 0o111) == (s.stat().st_mode & 0o111)
        if same:
            continue
        changes["changed" if (d.exists() or d.is_symlink()) else "added"].append(rel)
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_symlink() or d.exists():
            d.unlink()
        if s.is_symlink():
            os.symlink(os.readlink(s), d)
        else:
            shutil.copy2(s, d)

    # An added file the target's .gitignore swallows would silently never commit.
    new = changes["added"]
    if new:
        ignored = subprocess.run(["git", "-C", str(co), "check-ignore", "--stdin"], input="\n".join(new),
                                 capture_output=True, text=True).stdout.split()
        if ignored:
            raise SyncError(1, "exported files are ignored by the target's .gitignore: " + ", ".join(ignored))
    return changes


# ── 6. gates ─────────────────────────────────────────────────────────────────

VALIDATE_ROSTERS = """
import glob, sys
sys.path.insert(0, "adws")
from adw_modules import agents
paths = sorted(glob.glob("adws/adw_sssf_config/*.yaml"))
assert paths, "no rosters under adws/adw_sssf_config/"
for p in paths:
    cfg = agents.load_config(p)
    agents.validate(cfg, [a.name for a in cfg.agents])
    print("ok", p)
"""


def gates(cfg: dict) -> None:
    co, app = cfg["checkout"], cfg["app"]
    with tempfile.TemporaryDirectory(prefix=f"{cfg['name']}-build-") as outdir:
        steps = [
            ("manifest", ["uv", "run", "-q", "adws/adw_modules/manifest.py", "get", "source.repo"]),
            ("build", ["bun", "build", app.entry, "--outdir", outdir]),
            ("test", ["bun", "test", app.test_file]),
            ("rosters", ["uv", "run", "-q", "--with", "pydantic", "--with", "pyyaml", "--with", "python-dotenv",
                         "--with", "rich", "python", "-c", VALIDATE_ROSTERS]),
        ]
        for label, cmd in steps:
            r = subprocess.run(cmd, cwd=co, capture_output=True, text=True)
            if r.returncode != 0:
                tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
                raise SyncError(3, f"gate {label} failed: {' '.join(cmd)}\n{tail}")
            if label == "manifest" and r.stdout.strip() != manifest.load_target(cfg["name"]).source.repo:
                raise SyncError(3, f"gate manifest: checkout source.repo {r.stdout.strip()!r} != target file's")
            log(f"   gate {label:<8} ok")


# ── main ─────────────────────────────────────────────────────────────────────

def rollback(cfg: dict) -> None:
    co = cfg["checkout"]
    git(co, "reset", "--quiet", "--hard", "HEAD", check=False)
    git(co, "clean", "-fdq", "--", *cfg["sync_paths"], check=False)


def sync(name: str, push: bool, dry_run: bool) -> int:
    cfg = load_config(name)
    co, branch = cfg["checkout"], cfg["branch"]
    host_app = manifest.load(REPO_ROOT).app.name
    host_sha = git(REPO_ROOT, "rev-parse", "HEAD").stdout.strip()
    log(f"── target sync {name}{' (dry run)' if dry_run else ''} ── host {host_sha[:12]} -> {co}")

    log("[1/9] preconditions")
    preconditions(cfg)
    log(f"   pristine: {pristine_guard(cfg)}")

    with tempfile.TemporaryDirectory(prefix=f"target-sync-{name}-") as tmp:
        src = Path(tmp)
        log("[2/9] export + [3/9] derived exclusions")
        exported = export(cfg, src, host_app)
        log(f"   {len(exported)} tracked files under {', '.join(cfg['sync_paths'])}")

        patterns = forbidden_patterns(cfg, host_app)
        log(f"[4/9] leak check ({len(patterns)} patterns, derived + target.leak_patterns)")
        hits = leak_scan(src, list(exported), patterns)
        if hits:
            for h in hits:
                log(f"   LEAK {h}")
            raise SyncError(2, f"{len(hits)} leak hit(s) in the export — fix them at the source; checkout untouched")
        log("   clean")

        log("[5/9] mirror")
        try:
            changes = mirror(cfg, src, exported)
        except BaseException:
            rollback(cfg)
            raise
    try:
        for kind in ("added", "changed", "deleted"):
            log(f"   {kind:<8}{len(changes[kind]):>4}")
        for rel in changes["deleted"]:
            log(f"     - {rel}")
        git(co, "add", "-A", "--", *cfg["sync_paths"])
        stat = git(co, "diff", "--cached", "--stat").stdout.rstrip()
        if stat:
            log(stat)

        log("[6/9] gates")
        gates(cfg)
    except BaseException:
        rollback(cfg)
        raise

    if dry_run:
        rollback(cfg)
        log("dry run: checkout reset; nothing committed" + ("" if stat else " (nothing to commit)"))
        return 0

    log("[7/9] commit")
    if stat:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = git(co, "commit", "--quiet", "-m", f"factory sync {date}", check=False)
        if r.returncode != 0:
            rollback(cfg)
            raise SyncError(1, f"commit failed: {r.stderr.strip()}")
        log(f"   factory sync {date}")
    else:
        log("   nothing to commit — factory already matches HEAD")
    target_sha = git(co, "rev-parse", "HEAD").stdout.strip()

    code = 0
    if push:
        log(f"[8/9] push origin {branch}")
        r = git(co, "push", "--quiet", "origin", branch, check=False)
        if r.returncode != 0:
            log(f"   push failed: {r.stderr.strip()}")
            code = 4
        else:
            git(co, "fetch", "--quiet", "origin", branch, check=False)
    else:
        log("[8/9] push skipped (no --push)")
    pushed = git(co, "merge-base", "--is-ancestor", target_sha, f"origin/{branch}", check=False).returncode == 0

    log("[9/9] provenance")
    PROVENANCE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host_sha": host_sha,
        "target_sha": target_sha,
        "pushed": pushed,
    }
    out = PROVENANCE_DIR / f"{name}.json"
    out.write_text(json.dumps(record, indent=2) + "\n")
    log(f"   {out.relative_to(REPO_ROOT)}: {json.dumps(record)}")
    if not pushed:
        log("   not on the remote yet — a VM can't clone it; fill will run unpinned until --push")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("name", help="a targets/<name>.yaml stem")
    ap.add_argument("--push", action="store_true", help="push the sync commit (outward-facing)")
    ap.add_argument("--dry-run", action="store_true", help="export, check, mirror, gate — then reset")
    ap.add_argument("--check-pristine", action="store_true", help="only the pristine guard, read-only; exit 0 or 5")
    args = ap.parse_args()
    if sum((args.push, args.dry_run, args.check_pristine)) > 1:
        ap.error("--push, --dry-run and --check-pristine are mutually exclusive")
    try:
        if args.check_pristine:
            print(f"pristine: {pristine_guard(load_config(args.name))}")
            return 0
        return sync(args.name, args.push, args.dry_run)
    except SyncError as e:
        print(f"target sync: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
