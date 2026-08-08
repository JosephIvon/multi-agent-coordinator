"""Cross-repo contract version guard for the mac_coffee <-> mac-agent boundary.

mac_coffee depends on the mac-agent ``Registry`` surface behind
``MAC_AGENT_CONTRACT_VERSION`` (in ``mac_coffee/src/mac_coffee/agent_link.py``).
mac-agent mirrors that version in
``tests/contract_fixtures/mac_coffee_contract.json`` (``contract_version``) so the
two repos can detect drift without importing each other.

This script reads both and fails if they disagree. It is the release-time safety
net: bumping the contract on one side without the other now turns CI red instead of
shipping a silent protocol break. Signature-level drift is still covered by the
pytest suite (``test_cross_project_contract.py``); this check is the cheap,
always-on version-number gate.

Usage::

    # Local dev (mac_coffee checked out next to multi-agent-coordinator):
    python scripts/check_contract_sync.py --mac-coffee-path ../muti-agent-coffee

    # CI: checkout both repos and point --mac-coffee-path at the mac_coffee root.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "contract_fixtures" / "mac_coffee_contract.json"
MAC_COFFEE_CONST = re.compile(r"""MAC_AGENT_CONTRACT_VERSION\s*=\s*["']([^"']+)["']""")


def _read_mac_coffee_version(mac_coffee_root: Path) -> str | None:
    candidate = mac_coffee_root / "src" / "mac_coffee" / "agent_link.py"
    if not candidate.exists():
        print(f"ERROR: {candidate} not found", file=sys.stderr)
        return None
    text = candidate.read_text(encoding="utf-8")
    match = MAC_COFFEE_CONST.search(text)
    if not match:
        print(f"ERROR: MAC_AGENT_CONTRACT_VERSION not found in {candidate}", file=sys.stderr)
        return None
    return match.group(1)


def _read_fixture_version() -> str | None:
    if not FIXTURE.exists():
        print(f"ERROR: {FIXTURE} not found", file=sys.stderr)
        return None
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    version = data.get("contract_version")
    if version is None:
        print(f"ERROR: 'contract_version' missing in {FIXTURE}", file=sys.stderr)
        return None
    return str(version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check mac_coffee <-> mac-agent contract versions are in sync.")
    parser.add_argument(
        "--mac-coffee-path",
        default=None,
        help="Path to the mac_coffee repo root. If omitted, only the mac-agent fixture side is sanity-checked.",
    )
    args = parser.parse_args(argv)

    fixture_version = _read_fixture_version()
    if fixture_version is None:
        return 1

    if args.mac_coffee_path is None:
        print(
            f"WARN: --mac-coffee-path not provided; only verified mac-agent fixture "
            f"contract_version={fixture_version!r} is present. Pass --mac-coffee-path to "
            f"cross-check against mac_coffee.",
            file=sys.stderr,
        )
        print(f"OK: mac-agent fixture contract_version = {fixture_version!r}")
        return 0

    mac_coffee_version = _read_mac_coffee_version(Path(args.mac_coffee_path).resolve())
    if mac_coffee_version is None:
        return 1

    if mac_coffee_version != fixture_version:
        print(
            f"FAIL: contract version drift — mac_coffee MAC_AGENT_CONTRACT_VERSION="
            f"{mac_coffee_version!r} but mac-agent fixture contract_version={fixture_version!r}. "
            f"Bump both sides together (and re-run test_cross_project_contract.py).",
            file=sys.stderr,
        )
        return 1

    print(f"OK: contract in sync — version {fixture_version!r} (mac_coffee == mac-agent fixture)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
