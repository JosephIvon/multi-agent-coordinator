from __future__ import annotations

import tomllib
from pathlib import Path
import subprocess
import sys

import mac


ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_version_matches_project_metadata():
    project = _pyproject()["project"]

    assert mac.__version__ == project["version"]


def test_http_extra_declares_http_adapter_runtime_dependency():
    extras = _pyproject()["project"]["optional-dependencies"]

    assert "http" in extras
    assert any(requirement.startswith("fastapi") for requirement in extras["http"])


def test_dev_extra_contains_test_http_and_release_tooling():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]

    assert any(requirement.startswith("pytest") for requirement in dev)
    assert any(requirement.startswith("fastapi") for requirement in dev)
    assert any(requirement.startswith("httpx") for requirement in dev)
    assert any(requirement.startswith("build") for requirement in dev)
    assert any(requirement.startswith("twine") for requirement in dev)


def test_project_declares_console_script_entrypoint():
    scripts = _pyproject()["project"]["scripts"]

    assert scripts["mac-agent"] == "mac.cli:main"


def test_transport_exports_no_inprocess_wrapper_and_does_not_require_fastapi():
    script = r"""
import builtins

real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fastapi" or name.startswith("fastapi."):
        raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import mac.transport as transport
print(hasattr(transport, "InProcessMAC"))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**dict(__import__("os").environ), "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_transport_wildcard_import_does_not_require_fastapi():
    script = r"""
import builtins

real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fastapi" or name.startswith("fastapi."):
        raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
namespace = {}
exec("from mac.transport import *", namespace)
print(sorted(name for name in namespace if not name.startswith("__")))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**dict(__import__("os").environ), "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_release_smoke_script_is_documented_and_available():
    script = ROOT / "scripts" / "release_smoke.py"
    assert script.exists()


def test_readme_documents_install_verification_and_build_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pip install mac-agent" in readme
    assert "mac-agent[http]" in readme
    assert "python examples/local_handoff.py" in readme
    assert "python examples/local_runner.py" in readme
