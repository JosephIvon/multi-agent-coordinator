# P3-2: Mutation Testing Baseline

**Date:** 2026-07-22
**Status:** Complete
**Scope:** Mutation kill rate baseline for core modules (registry.py, quality/gate.py, storage/sqlite.py)

---

## Environment

- **mutmut**: 3.6.0
- **OS**: WSL2 Ubuntu (mutmut 3.x does not support native Windows; GitHub issue #397)
- **Python**: 3.14.4 (WSL venv)
- **Config**: `setup.cfg` with `only_mutate` + `pytest_add_cli_args_test_selection` + `mutate_only_covered_lines=true` (run per-module, config deleted after)

---

## Results

| Module | Mutants | Killed 🎉 | Survived 🙁 | Untested 🫥 | Kill Rate |
|--------|---------|-----------|-------------|-------------|-----------|
| `quality/gate.py` | 75 | 55 | 20 | 0 | **73.3%** |
| `storage/sqlite.py` | 387 | 265 | 122 | 0 | **68.5%** |
| `registry.py` | 758 | 418 | 336 | 4 | **55.4%** |
| **Total** | **1220** | **738** | **478** | **4** | **60.7%** |

**Overall mutation kill rate: 60.7%** (738 killed / 1216 tested; 4 untested excluded)

---

## Interpretation

- **gate.py (73.3%)** — Highest quality. Small, focused, well-tested. The quality gate logic is thoroughly covered.
- **sqlite.py (68.5%)** — Good. CAS (compare-and-swap) status transitions and audit trail are well-protected. Survivors likely in edge-case SQL serialization paths.
- **registry.py (55.4%)** — Weakest. This is the largest business-logic module (plans, dependencies, cycle detection, handoffs, conflicts). 336 surviving mutants indicate meaningful test gaps, especially in:
  - Dependency readiness logic
  - Conflict lifecycle paths
  - Handoff inheritance edge cases

### Benchmark context
Industry mutation testing kill rates (from literature, e.g. "Mutation Testing Advances: An Analysis and Survey" by Papadakis et al.):
- **< 50%**: Weak test suite
- **50-70%**: Adequate / typical
- **70-85%**: Strong
- **> 85%**: Excellent (rare without dedicated effort)

MAC at **60.7%** sits in the "adequate/typical" range. This is expected for an Alpha-stage project and is NOT a regression — it is the first measurement.

---

## Surviving Mutants (478 total) — Hotspots

Without a threshold gate, these are data points for future test improvement, not blockers. The highest-value targets for additional tests (by module):

1. **registry.py (336 survivors)** — Largest improvement opportunity. Cycle detection, dependency resolution, and conflict resolution paths have the most surviving mutants.
2. **sqlite.py (122 survivors)** — SQL serialization edge cases; some may be equivalent mutants (no behavioral change).
3. **gate.py (20 survivors)** — Minor; likely equivalent mutants in boolean logic.

---

## Reproduction

```bash
# In WSL2 Ubuntu, from project root:
python3 -m venv .mutmut-venv
.mutmut-venv/bin/pip install -e ".[dev]" mutmut

# Per-module config in setup.cfg:
cat > setup.cfg << "EOF"
[mutmut]
source_paths=src
only_mutate=
    src/mac/registry.py
pytest_add_cli_args_test_selection=
    tests/test_registry.py
    tests/test_phase_a_registry.py
mutate_only_covered_lines=true
EOF

.mutmut-venv/bin/python -m mutmut run --max-children 4
# Repeat with only_mutate + test_selection for gate.py and sqlite.py
```

**Note:** `mutate_only_covered_lines=true` is essential — without it, registry.py generates 1557 mutants (most untested 🫥), making the run impractically slow with no signal. The covered-lines mode produced 758 meaningful mutants.

---

## Decision

- **No hard threshold.** This is a baseline measurement only, per prior decision ("暂不引入" mutmut as a gate).
- **No CI integration.** mutmut requires WSL/Linux; cannot run on the Windows dev environment natively.
- **Actionable signal:** registry.py at 55.4% is the weakest module. Future test-writing sprints should target its surviving mutants first. A reasonable stretch goal would be lifting registry.py toward 65-70% (matching sqlite.py) by adding tests for dependency/conflict edge cases.
- **Re-run cadence:** Re-measure after significant registry.py test additions, or quarterly as a health check.

---

## Cleanup

All mutmut artifacts (`.mutmut-cache`, `mutants/`, `setup.cfg`, `.mutmut-venv/`, `pyproject.toml.bak`) were removed after the run. No project files were modified for the baseline (only `tests/test_mcp_server.py` was changed, for an unrelated ToolError test fix).
