from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mac.protocol.errors import QualityGateError
from mac.protocol.messages import AgentCard, TaskTransfer
from mac.registry import Registry


@dataclass
class TaskRunResult:
    status: str
    command: str
    evidence: list[str] = field(default_factory=list)
    output: str = ""
    error_code: str | None = None

    @classmethod
    def passed(cls, *, command: str, evidence: list[str] | None = None, output: str = "") -> TaskRunResult:
        return cls(status="passed", command=command, evidence=evidence or ["test_output"], output=output)

    @classmethod
    def failed(
        cls,
        *,
        command: str,
        error_code: str,
        evidence: list[str] | None = None,
        output: str = "",
    ) -> TaskRunResult:
        return cls(status="failed", command=command, evidence=evidence or ["test_output"], output=output, error_code=error_code)


class LocalAgentRunner:
    def __init__(
        self,
        *,
        registry: Registry,
        agent: AgentCard,
        capability: str,
        handler: Callable[[TaskTransfer], TaskRunResult | dict[str, Any]],
        project_context: str | None = None,
    ) -> None:
        self.registry = registry
        self.agent = agent
        self.capability = capability
        self.handler = handler
        self.project_context = project_context

    def run_once(self) -> TaskTransfer | None:
        self.registry.register(self.agent)
        claimed = self.registry.claim_next_task(
            agent_id=self.agent.agent_id,
            capability=self.capability,
            project_context=self.project_context,
            best_effort=False,
        )
        if claimed is None:
            return None

        started_at = time.monotonic()
        self.registry.start_task(claimed.task_id, self.agent.agent_id)
        try:
            result = _normalize_result(self.handler(claimed))
        except Exception as exc:
            duration = time.monotonic() - started_at
            failed = self.registry.fail_task(claimed.task_id, self.agent.agent_id, "HANDLER_ERROR", str(exc))
            self._record_outcome("failed", duration, "HANDLER_ERROR")
            return failed

        duration = time.monotonic() - started_at
        self.registry.submit_quality_result(
            claimed.task_id,
            {
                "agent_id": self.agent.agent_id,
                "command": result.command,
                "status": result.status,
                "evidence": result.evidence,
                "output": result.output,
                "error_code": result.error_code,
            },
        )
        if result.status == "passed":
            try:
                completed = self.registry.complete_task(claimed.task_id, self.agent.agent_id)
            except QualityGateError as exc:
                failed = self.registry.fail_task(claimed.task_id, self.agent.agent_id, "QUALITY_GATE_FAILED", str(exc))
                self._record_outcome("failed", duration, "QUALITY_GATE_FAILED")
                return failed
            self._record_outcome("succeeded", duration)
            return completed

        failed = self.registry.fail_task(
            claimed.task_id,
            self.agent.agent_id,
            result.error_code or "TASK_EXECUTION_FAILED",
            result.output,
        )
        self._record_outcome("failed", duration, failed.error_code)
        return failed

    def _record_outcome(self, status: str, duration: float, error_code: str | None = None) -> None:
        self.registry.record_task_outcome(
            agent_id=self.agent.agent_id,
            capability=self.capability,
            task_type=self.capability,
            status=status,
            duration_seconds=duration,
            error_code=error_code,
        )


def command_task_handler(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout_seconds: float = 60,
    evidence_on_success: list[str] | None = None,
) -> Callable[[TaskTransfer], TaskRunResult]:
    command_list = [str(part) for part in command]

    def handler(task: TaskTransfer) -> TaskRunResult:
        command_text = " ".join(command_list)
        try:
            completed = subprocess.run(
                command_list,
                cwd=str(cwd) if cwd is not None else None,
                timeout=timeout_seconds,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            return TaskRunResult.failed(command=command_text, error_code="COMMAND_TIMEOUT", output=output)

        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode == 0:
            return TaskRunResult.passed(
                command=command_text,
                evidence=evidence_on_success or ["test_output"],
                output=output,
            )
        return TaskRunResult.failed(command=command_text, error_code="COMMAND_FAILED", output=output)

    return handler


def _normalize_result(value: TaskRunResult | dict[str, Any]) -> TaskRunResult:
    if isinstance(value, TaskRunResult):
        return value
    return TaskRunResult(**value)
