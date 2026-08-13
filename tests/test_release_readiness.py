from __future__ import annotations

import importlib.util

try:
    import tomllib  # Python 3.11+ stdlib
except ImportError:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

import subprocess
import sys
from pathlib import Path
from shutil import copy2

import mac

ROOT = Path(__file__).resolve().parents[1]


def _release_smoke_module():
    spec = importlib.util.spec_from_file_location("release_smoke", ROOT / "scripts" / "release_smoke.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_version_matches_project_metadata():
    project = _pyproject()["project"]

    assert mac.__version__ == project["version"]


def test_http_extra_declares_http_adapter_runtime_dependency():
    extras = _pyproject()["project"]["optional-dependencies"]

    assert "http" in extras
    assert any(requirement.startswith("fastapi") for requirement in extras["http"])


def test_mcp_extras_remain_on_the_fastmcp_v1_compatibility_line():
    extras = _pyproject()["project"]["optional-dependencies"]

    for extra_name in ("mcp", "dev"):
        mcp_requirements = [requirement for requirement in extras[extra_name] if requirement.startswith("mcp")]
        assert mcp_requirements == ["mcp>=1.0,<2"]


def test_mcp_extras_cap_pydantic_settings_before_fastmcp_lifespan_warning():
    extras = _pyproject()["project"]["optional-dependencies"]

    for extra_name in ("mcp", "dev"):
        settings_requirements = [
            requirement
            for requirement in extras[extra_name]
            if requirement.startswith("pydantic-settings")
        ]
        assert settings_requirements == ["pydantic-settings>=2.5.2,<2.15"]


def _pytest_asyncio_marked_test_files(test_root: Path) -> list[Path]:
    marker = "pytest.mark." + "asyncio"
    return [
        path.relative_to(test_root)
        for path in test_root.rglob("test_*.py")
        if marker in path.read_text(encoding="utf-8")
    ]


def test_pytest_asyncio_guard_detects_nested_module_level_marker(tmp_path: Path):
    nested_tests = tmp_path / "nested"
    nested_tests.mkdir()
    marker = "pytest.mark." + "asyncio"
    (nested_tests / "test_async.py").write_text(
        f"import pytest\npytestmark = {marker}\n",
        encoding="utf-8",
    )

    assert _pytest_asyncio_marked_test_files(tmp_path) == [Path("nested/test_async.py")]


def test_test_suite_uses_stdlib_asyncio_without_pytest_asyncio_markers():
    assert _pytest_asyncio_marked_test_files(ROOT / "tests") == []


def test_doc_sync_rejects_drift_in_spec_and_integrations(tmp_path: Path):
    fixture_root = tmp_path / "repo"
    server_dir = fixture_root / "src" / "mac"
    docs_dir = fixture_root / "docs"
    server_dir.mkdir(parents=True)
    docs_dir.mkdir()
    copy2(ROOT / "src" / "mac" / "mcp_server.py", server_dir / "mcp_server.py")
    for relative_path in ("CLAUDE.md", "README.md", "docs/SPEC.md", "docs/INTEGRATIONS.md"):
        target = fixture_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(ROOT / relative_path, target)

    for relative_path, old, new in (
        ("docs/SPEC.md", "### Tools (31)", "### Tools (30)"),
        ("docs/INTEGRATIONS.md", "31 tools + 4 resources", "30 tools + 4 resources"),
    ):
        path = fixture_root / relative_path
        path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_doc_sync.py"), "--root", str(fixture_root)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "docs/SPEC.md claims 30 tools" in result.stderr
    assert "docs/INTEGRATIONS.md claims 30 tools" in result.stderr


def test_dev_extra_contains_test_http_and_release_tooling():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]

    assert any(requirement.startswith("pytest") for requirement in dev)
    assert any(requirement.startswith("fastapi") for requirement in dev)
    assert any(requirement.startswith("httpx") for requirement in dev)
    assert any(requirement.startswith("build") for requirement in dev)
    assert any(requirement.startswith("hatchling") for requirement in dev)
    assert any(requirement.startswith("twine") for requirement in dev)


def test_project_declares_console_script_entrypoint():
    scripts = _pyproject()["project"]["scripts"]

    assert scripts["mac-agent"] == "mac.cli:main"


def test_project_declares_all_release_console_entrypoints():
    scripts = _pyproject()["project"]["scripts"]

    assert scripts["mac-mcp-server"] == "mac.mcp_server:main"
    assert scripts["mac-http-server"] == "mac.transport.http_ws:main"


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


def test_release_smoke_script_covers_mcp_extra_and_entrypoint():
    source = (ROOT / "scripts" / "release_smoke.py").read_text(encoding="utf-8")

    assert 'f"{wheel}[mcp]"' in source
    assert 'from mac.mcp_server import mcp' in source
    assert '_venv_script(venv, "mac-mcp-server")' in source


def test_release_smoke_defaults_to_isolated_dependency_resolution(monkeypatch, tmp_path: Path):
    smoke = _release_smoke_module()
    commands: list[list[str]] = []
    wheel = tmp_path / "mac_agent.whl"

    def fake_run(command, **kwargs):
        commands.append(command)
        stdout = '{"risk_level": "low"}' if kwargs.get("capture_output") else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    def fake_script(_venv, name):
        path = tmp_path / name
        path.touch()
        return path

    monkeypatch.setattr(smoke, "_latest_wheel", lambda: wheel)
    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(smoke, "_venv_script", fake_script)

    assert smoke.main(["--skip-build"]) == 0

    venv_command = next(command for command in commands if command[:3] == [sys.executable, "-m", "venv"])
    pip_commands = [command for command in commands if command[1:4] == ["-m", "pip", "install"]]
    assert "--system-site-packages" not in venv_command
    assert all("--no-deps" not in command for command in pip_commands)

    commands.clear()
    assert smoke.main(["--skip-build", "--fast-local"]) == 0

    venv_command = next(command for command in commands if command[:3] == [sys.executable, "-m", "venv"])
    pip_commands = [command for command in commands if command[1:4] == ["-m", "pip", "install"]]
    assert "--system-site-packages" in venv_command
    assert all("--no-deps" in command for command in pip_commands)


def test_release_smoke_never_falls_back_to_a_global_console_script():
    source = (ROOT / "scripts" / "release_smoke.py").read_text(encoding="utf-8")

    assert "shutil.which" not in source


def test_readme_documents_install_verification_and_build_commands():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pip install mac-agent" in readme
    assert "mac-agent[http]" in readme
    assert "python examples/local_handoff.py" in readme
    assert "python examples/local_runner.py" in readme
    assert "python examples/collaboration_plan.py" in readme


def test_install_guide_pins_the_current_project_version():
    project_version = _pyproject()["project"]["version"]
    install_guide = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")

    assert f'mac-agent[http,mcp]=={project_version}' in install_guide


def test_readme_collaboration_quick_start_commands_are_valid(tmp_path):
    from mac.cli import main

    db_path = tmp_path / "mac.db"
    commands = [
        ["plan", "create", "--db", str(db_path), "--plan-id", "plan-1", "--goal", "Ship login flow", "--created-by", "planner"],
        ["plan", "activate", "--db", str(db_path), "--plan-id", "plan-1"],
        ["register", "--db", str(db_path), "--agent-id", "coder", "--name", "Coder", "--capability", "write_code", "--allowed-path", "src/**"],
        ["register", "--db", str(db_path), "--agent-id", "tester", "--name", "Tester", "--capability", "write_test", "--allowed-path", "tests/**"],
        ["submit", "--db", str(db_path), "--task-id", "code-login", "--source-agent-id", "planner", "--type", "write_code", "--summary", "Implement login", "--plan-id", "plan-1"],
        [
            "submit",
            "--db",
            str(db_path),
            "--task-id",
            "test-login",
            "--source-agent-id",
            "planner",
            "--type",
            "write_test",
            "--summary",
            "Test login",
            "--plan-id",
            "plan-1",
            "--depends-on",
            "code-login",
            "--target-module",
            "src/login.py",
            "--coverage-goal",
            "80",
        ],
        ["ready-tasks", "--db", str(db_path), "--capability", "write_code"],
        ["worker-packet", "--db", str(db_path), "--task-id", "code-login", "--agent-id", "coder"],
    ]

    for command in commands:
        assert main(command) == 0
