"""Project-wide pytest configuration.

Installed early (at collection time, before any test module imports fastapi/
starlette) so it can suppress the *import-time* ``StarletteDeprecationWarning``
that ``fastapi.testclient`` raises when starlette >= 1.3 is present. The
``[tool.pytest.ini_options] filterwarnings`` entry only catches warnings emitted
during test execution, not during import, which is why this early filter is
needed as well.

The real fix is pinning ``starlette<1.3`` in the ``dev``/``http`` extras (see
pyproject.toml); this is the safety net for already-installed 1.3.x environments.
"""

import warnings

import starlette.exceptions


def pytest_configure(config):
    # Installed during pytest's configure phase (after pytest's own warning-filter
    # reset) so it survives and suppresses the import-time StarletteDeprecationWarning
    # that fastapi.testclient raises when starlette >= 1.3 is present. The real fix is
    # pinning `starlette<1.3` in the dev/http extras; this is the fallback for
    # already-installed 1.3.x environments.
    warnings.filterwarnings(
        "ignore",
        category=starlette.exceptions.StarletteDeprecationWarning,
    )
