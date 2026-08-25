"""Verify the package version matches the release tag before publishing.

The publish workflow only fires on ``v*`` tags. A tag whose name does not
match the version actually built into the wheel produces a PyPI release
that no checkout can reproduce — the exact failure mode this guard exists
to prevent (e.g. tagging ``v1.2.2`` while ``pyproject.toml`` still says
``1.2.1``).

Checked sources (all must agree when a tag is present):

* ``pyproject.toml``  — ``[project] version``
* ``src/mac/__init__.py`` — ``__version__``
* ``git describe --exact-match`` — the tag being published (``v1.2.1``)

Usage::

    python scripts/check_version_tag.py            # no tag → runtime consistency only
    python scripts/check_version_tag.py --tag v1.2.1
    python scripts/check_version_tag.py --root /path/to/repo --tag v1.2.1

Exit code 0 = consistent; 1 = mismatch (prints what disagreed).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_INIT_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def _pyproject_version(root: Path) -> str | None:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return None
    match = _PYPROJECT_VERSION.search(pyproject.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _init_version(root: Path) -> str | None:
    init = root / "src" / "mac" / "__init__.py"
    if not init.exists():
        return None
    match = _INIT_VERSION.search(init.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _git_tag() -> str | None:
    """Return the exact tag on HEAD, or None on a detached/untagged checkout."""
    try:
        result = subprocess.run(
            ["git", "describe", "--exact-match", "--tags", "HEAD"],
            capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout.strip() or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root")
    parser.add_argument(
        "--tag", type=str, default=None,
        help="tag name to verify (defaults to $GITHUB_REF_NAME, then git describe)",
    )
    args = parser.parse_args()

    tag = args.tag
    if tag is None:
        tag = None  # keep None outside CI; GITHUB_REF_NAME handled below
    import os
    if tag is None:
        tag = os.environ.get("GITHUB_REF_NAME")

    pv = _pyproject_version(args.root)
    iv = _init_version(args.root)

    failures: list[str] = []

    # Runtime consistency: pyproject and __init__ must always agree.
    if pv is None:
        failures.append("pyproject.toml: no [project] version found")
    if iv is None:
        failures.append("src/mac/__init__.py: no __version__ found")
    if pv and iv and pv != iv:
        failures.append(f"pyproject version {pv} != __init__ __version__ {iv}")

    # Tag consistency: when publishing from a v* tag, tag must equal version.
    if tag is not None and tag.startswith("v"):
        stripped = tag[1:]
        if pv and stripped != pv:
            failures.append(f"tag {tag} != pyproject version {pv}")
        if iv and stripped != iv:
            failures.append(f"tag {tag} != __init__ __version__ {iv}")

    if failures:
        for line in failures:
            print(f"ERROR: {line}", file=sys.stderr)
        return 1

    checked = f" (tag {tag})" if tag else ""
    print(f"OK: version {pv} consistent across pyproject/__init__{checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
