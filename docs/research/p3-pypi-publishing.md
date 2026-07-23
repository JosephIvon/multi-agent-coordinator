# A3: PyPI Publishing Research & Setup

**Date:** 2026-07-23
**Status:** Ready — metadata complete, build verified, publish procedure documented
**Package:** `mac-agent` (v0.4.0)

---

## 1. Summary

The project is now publish-ready. This document records the verification done
and the exact procedure to release to PyPI.

---

## 2. Metadata Gaps Found & Fixed

The initial `pyproject.toml` was missing three fields PyPI requires/recommends:

| Field | Before | After |
|-------|--------|-------|
| `authors` | missing | `[{name = "Wenbo Fan", email = "fanwenbo_best@126.com"}]` |
| `license` | missing (README says MIT, not declared) | `license = "MIT"` → `License-Expression: MIT` |
| `project.urls` | missing | Homepage, Documentation, Repository, Changelog, Issues |

Also fixed a TOML structural bug: `dependencies` was placed after
`[project.urls]`, so TOML parsed it as a key under `[project.urls]` (hatchling
error: `URL dependencies of field project.urls must be a string`). Moved
`dependencies` above `[project.urls]` within the `[project]` table.

---

## 3. Build Verification (Local)

```bash
pip install build twine       # already in [dev] extra
python -m build               # → dist/mac_agent-0.4.0.tar.gz + .whl
twine check dist/*            # PASSED (both)
```

Resulting wheel metadata confirmed:
- `Name: mac-agent`
- `Version: 0.4.0`
- `Author-email: Wenbo Fan <fanwenbo_best@126.com>`
- `License-Expression: MIT`
- 5 `Project-URL` entries

`test_release_readiness.py` already guards version-consistency, dev extra,
console scripts, and README install commands. All pass (218 tests).

---

## 4. Publish Procedure

### 4.1 Prerequisites (one-time)

1. PyPI account + API token (scope: "Entire account", name `mac-agent-publish`)
2. Store token securely (env var or secret manager) — **never commit it**
3. 2FA enabled on PyPI account

### 4.2 Test on TestPyPI first (recommended)

```bash
# Build
python -m build
twine check dist/*

# Upload to TestPyPI
twine upload --repository testpypi dist/*

# Verify install works from TestPyPI
pip install -i https://test.pypi.org/simple/ mac-agent
mac-agent --help
mac-mcp-server --help
```

TestPyPI is a separate index; resolves real dependencies from PyPI but the
package itself from TestPyPI. Catches metadata/upload issues without polluting
the real index.

### 4.3 Publish to PyPI

```bash
# Same dist artifacts (rebuild if stale)
python -m build
twine check dist/*

twine upload dist/*
```

Once uploaded, PyPI versions are **immutable** — you cannot re-upload the same
version. Verify on TestPyPI first.

### 4.4 Verify

```bash
pip install mac-agent
pip install "mac-agent[mcp]"
pip install "mac-agent[http]"
mac-agent --help
python -c "import mac; print(mac.__version__)"
```

---

## 5. CI Automation (Deferred — Phase B+)

A GitHub Actions workflow can automate publishing on tag push:

```yaml
# .github/workflows/publish.yml (NOT yet created — Phase B+)
name: Publish
on:
  push:
    tags: ['v*']
jobs:
  build-and-publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install build twine
      - run: python -m build
      - run: twine check dist/*
      - run: twine upload dist/*
        env:
          TWINE_API_TOKEN: ${{ secrets.PYPI_API_TOKEN }}
```

**Why deferred:** Manual publish for Alpha is fine and gives a human checkpoint
before each release. CI auto-publish is worth setting up once the release
cadence stabilises (post-Phase B). A CI workflow for **tests** (not publish)
is higher priority — see Phase B design doc B-adjacent concern.

---

## 6. Pre-Publish Checklist

Before each release:

- [ ] `python -m pytest tests/ -q` all green
- [ ] `python -m build && twine check dist/*` PASSED
- [ ] Version bumped in `pyproject.toml` + `src/mac/__init__.py` (must match)
- [ ] `git tag -a vX.Y.Z` created
- [ ] CHANGELOG updated (GitHub Releases page serves as changelog via Project-URL)
- [ ] TestPyPI install verified (first time / major version only)

---

## 7. `.gitignore` for build artifacts

`dist/`, `build/`, `*.egg-info/` should be gitignored. Verified current
`.gitignore` already excludes these (cleaned up in commit `258c6d8`).

---

## 8. Decision

**Do not publish v0.4.0 to PyPI yet.** Rationale:

- Alpha stage, API may still shift during Phase B
- No CI test workflow yet — publishing without automated multi-version test
  matrix is risky
- Local `pip install -e .` is sufficient for current single-workspace usage

**Publish target: v0.5.0** (after Phase B lands and a CI test workflow exists).
The metadata and procedure are ready now; only the trigger is deferred.

---

*Research document: A3 — PyPI Publishing*
