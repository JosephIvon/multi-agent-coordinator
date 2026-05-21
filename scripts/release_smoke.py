from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the package and smoke-test the wheel in a clean venv.")
    parser.add_argument("--skip-build", action="store_true", help="Use an existing dist/*.whl instead of building first")
    parser.add_argument(
        "--resolve-deps",
        action="store_true",
        help="Install wheel dependencies from package indexes instead of using the current environment.",
    )
    args = parser.parse_args(argv)

    if not args.skip_build:
        _run([sys.executable, "-m", "build", "--no-isolation"], cwd=ROOT)

    wheel = _latest_wheel()
    with tempfile.TemporaryDirectory(prefix="mac-agent-release-") as temp_dir:
        venv = Path(temp_dir) / "venv"
        venv_command = [sys.executable, "-m", "venv"]
        if not args.resolve_deps:
            venv_command.append("--system-site-packages")
        _run([*venv_command, str(venv)])
        python = _venv_python(venv)
        pip_install = [str(python), "-m", "pip", "install", "--timeout", "120", "--retries", "3"]
        if not args.resolve_deps:
            pip_install.append("--no-deps")
        _run([*pip_install, str(wheel)])
        _run([str(python), "-c", "import mac; print(mac.__version__)"])
        _run(
            [
                str(python),
                "-c",
                (
                    "import mac, pathlib, sys; "
                    "path = pathlib.Path(mac.__file__).resolve(); "
                    "prefix = pathlib.Path(sys.prefix).resolve(); "
                    "print(path); "
                    "assert path.is_relative_to(prefix), (path, prefix)"
                ),
            ]
        )
        _run([str(_venv_script(venv, "mac-agent")), "--help"])
        result = _run([str(_venv_script(venv, "mac-agent")), "contract", "--risk", "low"], capture_output=True)
        contract = json.loads(result.stdout)
        if contract["risk_level"] != "low":
            raise SystemExit("contract smoke test returned unexpected risk_level")

        _run([str(python), "-c", "from mac.registry import Registry; print(Registry.__name__)"])
        _run([str(python), "-c", "from mac.events import TaskEventBus; print(TaskEventBus.__name__)"])
        _run([*pip_install, f"{wheel}[http]"])
        _run([str(python), "-c", "from mac.transport.http_ws import create_app; print(create_app.__name__)"])

    return 0


def _latest_wheel() -> Path:
    wheels = sorted((ROOT / "dist").glob("*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise SystemExit("No wheel found in dist/. Run python -m build first.")
    return wheels[-1]


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        check=True,
    )


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_script(venv: Path, name: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    script_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
    path = script_dir / f"{name}{suffix}"
    if path.exists():
        return path
    fallback = shutil.which(name)
    if fallback:
        return Path(fallback)
    return path


if __name__ == "__main__":
    raise SystemExit(main())
