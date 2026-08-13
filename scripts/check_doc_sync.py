"""Verify the documented MCP surface matches the code.

The MCP tool/resource count lives in three places that must stay in lock-step:

* ``src/mac/mcp_server.py``  — the actual ``@mcp.tool()`` / ``@mcp.resource()`` decorators
* ``CLAUDE.md`` (§9)         — claims ``N tools + M resources``
* ``README.md``              — claims ``N tools + M resources`` in the layout block
* ``docs/SPEC.md``           — declares the complete MCP tool surface
* ``docs/INTEGRATIONS.md``   — declares the integration MCP surface

This guard catches the exact drift that happened before (docs claimed
``16 tools + 2 resources`` while the server shipped 31 + 4). Run it in CI so a
forgotten doc edit turns the build red instead of shipping stale numbers.

Usage::

    python scripts/check_doc_sync.py
    python scripts/check_doc_sync.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_TOOL_DECORATOR = re.compile(r"^\s*@mcp\.tool\(\s*\)\s*$")
# Resources are registered with a URI argument, e.g. @mcp.resource("mac://health").
_RESOURCE_DECORATOR = re.compile(r'^\s*@mcp\.resource\(\s*["\']')
# Matches "31 tools + 4 resources" anywhere in prose.
_COUNT_CLAIM = re.compile(r"(\d+)\s+tools?\s*\+\s*(\d+)\s+resources?", re.IGNORECASE)
_SPEC_TOOLS_HEADING = re.compile(r"^###\s+Tools\s+\((\d+)\)\s*$", re.MULTILINE)
_SPEC_RESOURCES_HEADING = re.compile(r"^###\s+Resources\s+\((\d+)\)\s*$", re.MULTILINE)


def _count_decorators(server_path: Path) -> tuple[int, int]:
    tools = resources = 0
    for line in server_path.read_text(encoding="utf-8").splitlines():
        if _TOOL_DECORATOR.match(line):
            tools += 1
        elif _RESOURCE_DECORATOR.match(line):
            resources += 1
    return tools, resources


def _claim_in(path: Path) -> tuple[int, int] | None:
    text = path.read_text(encoding="utf-8")
    match = _COUNT_CLAIM.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    tools = _SPEC_TOOLS_HEADING.search(text)
    resources = _SPEC_RESOURCES_HEADING.search(text)
    if tools and resources:
        return int(tools.group(1)), int(resources.group(1))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check documented MCP tool/resource counts match the code.")
    parser.add_argument("--root", default=str(REPO_ROOT), help="Repository root (default: repo of this script)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    server = root / "src" / "mac" / "mcp_server.py"
    claude = root / "CLAUDE.md"
    readme = root / "README.md"
    spec = root / "docs" / "SPEC.md"
    integrations = root / "docs" / "INTEGRATIONS.md"

    if not server.exists():
        print(f"ERROR: {server} not found", file=sys.stderr)
        return 1

    actual_tools, actual_resources = _count_decorators(server)
    errors: list[str] = []

    # Code is the source of truth; docs must agree with it.
    for doc, doc_path in (
        ("CLAUDE.md", claude),
        ("README.md", readme),
        ("docs/SPEC.md", spec),
        ("docs/INTEGRATIONS.md", integrations),
    ):
        if not doc_path.exists():
            print(f"WARN: {doc_path} not found, skipping", file=sys.stderr)
            continue
        claim = _claim_in(doc_path)
        if claim is None:
            print(f"WARN: no 'N tools + M resources' claim found in {doc}, skipping", file=sys.stderr)
            continue
        claimed_tools, claimed_resources = claim
        if claimed_tools != actual_tools:
            errors.append(f"{doc} claims {claimed_tools} tools but code has {actual_tools}")
        if claimed_resources != actual_resources:
            errors.append(f"{doc} claims {claimed_resources} resources but code has {actual_resources}")

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        print(
            f"\nActual MCP surface: {actual_tools} tools + {actual_resources} resources "
            f"(from {server.name}). Update CLAUDE.md, README.md, docs/SPEC.md, and docs/INTEGRATIONS.md to match.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: MCP surface in sync — {actual_tools} tools + {actual_resources} resources "
        "across code/CLAUDE.md/README.md/docs/SPEC.md/docs/INTEGRATIONS.md"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
