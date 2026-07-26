from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from mac.adapters.lifecycle import AgentResult
from mac.adapters.protocol import AdapterManifest, PreparedContext


class GenericContextAdapter:
    """Fallback adapter for any IDE that can read a project context file."""

    manifest = AdapterManifest("generic-context", "Generic Context File", capabilities=frozenset({"context_file"}))

    def prepare_context(self, *, task_id: str, context: str, output_dir: Path) -> PreparedContext:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"task-{task_id}.md"
        path.write_text(context, encoding="utf-8")
        return PreparedContext(task_id=task_id, content=context, path=path)


@dataclass(frozen=True)
class CliDispatchResult:
    returncode: int
    stdout: str
    stderr: str


class GenericCliAdapter(GenericContextAdapter):
    """Adapter for tools that accept a prompt/context file on the command line."""

    manifest = AdapterManifest(
        "generic-cli",
        "Generic CLI Agent",
        capabilities=frozenset({"context_file", "cli_dispatch"}),
    )

    def dispatch(
        self,
        prepared: PreparedContext,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout: float = 3600,
        cancel_event: Event | None = None,
    ) -> CliDispatchResult:
        if prepared.path is None:
            raise ValueError("CLI dispatch requires a materialized context path")
        rendered = [part.replace("{context_file}", str(prepared.path)) for part in command]
        process = subprocess.Popen(rendered, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, shell=False)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                return CliDispatchResult(130, stdout, (stderr + "\nMAC: command cancelled").strip())
            if time.monotonic() >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                return CliDispatchResult(124, stdout, (stderr + "\nMAC: command timed out").strip())
            time.sleep(0.05)
        stdout, stderr = process.communicate()
        return CliDispatchResult(process.returncode or 0, stdout, stderr)

    @staticmethod
    def normalize_result(result: CliDispatchResult) -> AgentResult:
        from mac.security import redact
        status = "completed" if result.returncode == 0 else "failed"
        return AgentResult(
            status=status,
            summary=redact(result.stdout[-4000:]),
            raw=redact({"returncode": result.returncode, "stderr": result.stderr[-4000:]}),
        )

    @staticmethod
    def command_from_string(command: str) -> list[str]:
        return shlex.split(command, posix=False)


class GenericMcpAdapter(GenericContextAdapter):
    """Manifest-only adapter for tools that connect to MAC through MCP."""

    manifest = AdapterManifest("generic-mcp", "Generic MCP Client", capabilities=frozenset({"context_file", "mcp"}))

