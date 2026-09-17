# /// script
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Reader for app.manifest.yaml — the payload app's declared identity.

The manifest lives at the repo root so nothing has to know which dir under
apps/ is active before it can read the file that says which dir is active.
`just app swap` edits the YAML; every consumer reads it through here:

- Python:  ``from .manifest import load`` (quality.py)
- Bash:    ``uv run adws/adw_modules/manifest.py get source.repo`` (fill.just,
           observe.just)

Named targets. The root manifest is the implicit ``default`` target. Every
``targets/<name>.yaml`` is another one: the same ``app:`` + ``source:`` sections,
plus a host-only ``target:`` section that ``sandbox_mount/host/target_sync.py``
and harvest read. Lifecycle recipes pass the run record's target explicitly:

    manifest.py [--target NAME] get <dotted.key>
    manifest.py list                               default + every targets/*.yaml stem
    manifest.py get-target-section NAME <key>      a scalar or list under target:

No relative imports in this file — a file run by path has no package context,
and the CLI must work standalone under `uv run`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel

MANIFEST_NAME = "app.manifest.yaml"
DEFAULT_TARGET = "default"
# adws/adw_modules/manifest.py -> repo root. targets/ is host-only, so on a VM
# (or in a target checkout) the directory is simply absent and only `default` exists.
TARGETS_DIR = Path(__file__).resolve().parents[2] / "targets"
# A name, never a path: it is interpolated into a filename from shell input.
_TARGET_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class AppSection(BaseModel):
    name: str
    dir: str
    entry: str
    test_file: str
    generated_tests_dir: str


class SourceSection(BaseModel):
    repo: str


class Manifest(BaseModel):
    app: AppSection
    source: SourceSection


def _find_manifest(start: Path) -> Path:
    for candidate in (start, *start.parents):
        path = candidate / MANIFEST_NAME
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"{MANIFEST_NAME} not found walking up from {start} — it belongs at the "
        f"repo root; `just app swap` should have left one there"
    )


def load(repo_root: Path | None = None) -> Manifest:
    """Load the manifest, walking up from repo_root (or this module) to find it."""
    start = (repo_root or Path(__file__).parent).resolve()
    path = _find_manifest(start)
    return Manifest.model_validate(yaml.safe_load(path.read_text()))


def target_names() -> list[str]:
    """`default` first, then every targets/*.yaml stem that is a valid name."""
    stems = sorted(p.stem for p in TARGETS_DIR.glob("*.yaml")) if TARGETS_DIR.is_dir() else []
    return [DEFAULT_TARGET, *(s for s in stems if _TARGET_NAME.fullmatch(s))]


def _target_path(name: str) -> Path:
    path = TARGETS_DIR / f"{name}.yaml"
    if not _TARGET_NAME.fullmatch(name) or not path.is_file():
        raise ValueError(f"unknown target {name!r}; known: {', '.join(target_names())}")
    return path


def load_target(name: str, repo_root: Path | None = None) -> Manifest:
    """The manifest for a named target; `default` is the root app.manifest.yaml."""
    if name == DEFAULT_TARGET:
        return load(repo_root)
    # Manifest ignores the extra `target:` key (pydantic's default extra="ignore").
    return Manifest.model_validate(yaml.safe_load(_target_path(name).read_text()))


def target_section(name: str) -> dict:
    """The raw host-only `target:` section of a named target."""
    if name == DEFAULT_TARGET:
        raise ValueError("the default target has no target: section")
    section = (yaml.safe_load(_target_path(name).read_text()) or {}).get("target")
    if not isinstance(section, dict):
        raise ValueError(f"targets/{name}.yaml has no target: section")
    return section


def _get(manifest: Manifest, dotted_key: str) -> str:
    node: object = manifest
    for part in dotted_key.split("."):
        if not isinstance(node, BaseModel) or part not in type(node).model_fields:
            raise KeyError(f"unknown manifest key: {dotted_key!r}")
        node = getattr(node, part)
    if isinstance(node, BaseModel):
        raise KeyError(f"{dotted_key!r} is a section, not a scalar — name a field inside it")
    return str(node)


USAGE = """usage: manifest.py [--target NAME] get <dotted.key>   e.g. get source.repo
       manifest.py list
       manifest.py get-target-section NAME <key>   e.g. get-target-section greenfield checkout"""


def main(argv: list[str]) -> int:
    target = DEFAULT_TARGET
    if argv[:1] == ["--target"] and len(argv) >= 2:
        target, argv = argv[1], argv[2:]
    elif argv[:1] and argv[0].startswith("--target="):
        target, argv = argv[0].split("=", 1)[1], argv[1:]
    try:
        if argv == ["list"] and target == DEFAULT_TARGET:
            print("\n".join(target_names()))
            return 0
        if len(argv) == 2 and argv[0] == "get":
            # The CLI is invoked by path from recipes, so the caller's cwd — not this
            # file's location — is the checkout whose manifest is authoritative.
            print(_get(load_target(target, Path.cwd()), argv[1]))
            return 0
        if len(argv) == 3 and argv[0] == "get-target-section" and target == DEFAULT_TARGET:
            value = target_section(argv[1]).get(argv[2])
            if value is None:
                raise KeyError(f"targets/{argv[1]}.yaml target: has no {argv[2]!r}")
            # Lists one item per line, so bash can read them with mapfile.
            print("\n".join(map(str, value)) if isinstance(value, list) else value)
            return 0
        print(USAGE, file=sys.stderr)
        return 1
    except (FileNotFoundError, KeyError, ValueError) as error:
        print(f"manifest: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
