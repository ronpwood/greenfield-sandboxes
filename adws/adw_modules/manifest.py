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

No relative imports in this file — a file run by path has no package context,
and the CLI must work standalone under `uv run`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel

MANIFEST_NAME = "app.manifest.yaml"


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


def _get(manifest: Manifest, dotted_key: str) -> str:
    node: object = manifest
    for part in dotted_key.split("."):
        if not isinstance(node, BaseModel) or part not in type(node).model_fields:
            raise KeyError(f"unknown manifest key: {dotted_key!r}")
        node = getattr(node, part)
    if isinstance(node, BaseModel):
        raise KeyError(f"{dotted_key!r} is a section, not a scalar — name a field inside it")
    return str(node)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "get":
        print(f"usage: {Path(sys.argv[0]).name} get <dotted.key>   e.g. get source.repo",
              file=sys.stderr)
        return 1
    try:
        # The CLI is invoked by path from recipes, so the caller's cwd — not this
        # file's location — is the checkout whose manifest is authoritative.
        print(_get(load(Path.cwd()), argv[1]))
        return 0
    except (FileNotFoundError, KeyError, ValueError) as error:
        print(f"manifest: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
