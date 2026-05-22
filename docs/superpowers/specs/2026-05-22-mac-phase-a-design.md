# MAC Phase A – Plan + Collaboration Layer

> Note: this broad design was superseded for implementation by
> `2026-05-22-mac-phase-a-minimal-design.md` plus the amendments in that file.
> In particular, `accepted` does not unlock dependencies in the implemented
> Phase A behavior.

> 版本：1.0
> 日期：2026-05-22
> 状态：已批准
> 基于：`finance-ai-system` `.agent-state/` 原型的第一阶段实现经验

---

## 1. 背景与目标

MAC 当前是"任务状态机"：可以 submit、claim、start、complete/fail，覆盖了单任务生命周期。但真实的 multi-agent 协作是一组任务在一个 plan 上下文中的协作——有依赖、有并行、有 review、有冲突。

`.agent-state/` 原型验证了真实协作需要什么。Phase A 的目标是把 MAC 从"任务状态机"升级成"协作协调层"，承接 `.agent-state/` 的真实流程闭环。

**不做**：不追求自动运行 agent、不追求 gRPC/Redis/云端。Phase A 是纯本地协调增强。

---

## 2. 新增数据模型

### 2.1 Plan（协调单位）

```python
class Plan(BaseModel):
    plan_id: str
    goal: str
    status: Literal["draft", "active", "completed", "cancelled"] = "draft"
    parallel_groups: list[ParallelGroup] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    created_by: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    closed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParallelGroup(BaseModel):
    id: str
    family: str = ""
    round: str = ""
    task_ids: list[str] = Field(default_factory=list)
```

**关键语义**：
- `status=draft` 时 plan 可以修改；`status=active` 后只读
- `task_ids` 定义 plan 包含的任务，但不是所有任务都立即 ready
- `parallel_groups` 支持"两个 worker 完成后 Codex synthesis 才能开始"的模式

### 2.2 TaskDependency（依赖解锁）

```python
class TaskDependency(BaseModel):
    task_id: str
    depends_on: list[str] = Field(default_factory=list)  # task_ids this task waits for
    blocked_by: list[str] = Field(default_factory=list)  # computed from depends_on
    preferred_agents: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    validation: list[ValidationRule] = Field(default_factory=list)  # from .agent-state
    owned_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)


class ValidationRule(BaseModel):
    command: str = ""
    required: bool = False
    blocking: bool = False
    description: str = ""
```

**ready 计算规则**：
1. `status == "queued"` 且不在任何 `blocked_by` 列表中
2. 所有 `depends_on` 任务状态 ∈ `{"completed", "accepted", "cancelled"}`
3. 可选：Agent 匹配 `preferred_agents` 或 `required_capabilities`

### 2.3 AgentCard Path Permissions（文件边界）

```python
class AgentCard(BaseModel):
    agent_id: str
    name: str
    role: Literal["planner_reviewer", "runtime_executor", "artifact_executor"] = "runtime_executor"
    capabilities: list[AgentCapability] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)   # e.g. ["backend/**", "tests/**"]
    forbidden_paths: list[str] = Field(default_factory=list)  # e.g. ["db/**", "fixtures/gold/**"]
    load: int = Field(default=0, ge=0, le=100)
    status: str = "online"
    last_heartbeat: float = 0
    project_context: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 2.4 HandoffResult（结构化交接）

```python
class HandoffResult(BaseModel):
    task_id: str
    plan_id: str | None = None
    agent_id: str
    verification: list[VerificationEntry] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    docs_touched: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    boundary_review: Literal["pass", "block", "pending"] = "pending"
    violated_guardrail: list[str] = Field(default_factory=list)  # e.g. ["R1", "R3"]
    residual_risk: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class VerificationEntry(BaseModel):
    command: str
    result: str  # e.g. "PASS", "FAIL"
    description: str = ""
```

### 2.5 ConflictRecord（冲突记录）

```python
class ConflictRecord(BaseModel):
    conflict_id: str = Field(default_factory=lambda: str(uuid4()))
    plan_id: str | None = None
    task_id: str | None = None
    source: Literal["path_violation", "concurrent_ownership", "stale_task", "reject_reason", "blocker"] = "path_violation"
    severity: Literal["blocking", "non_blocking"] = "non_blocking"
    description: str = ""
    involved_agents: list[str] = Field(default_factory=list)
    involved_files: list[str] = Field(default_factory=list)
    resolved: bool = False
    resolution: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
```

---

## 3. Review Lifecycle（新增任务状态 + 操作）

### 3.1 扩展状态机

```
                    ┌─ review_ready ──┐
                    │        ↑        │
proposed → accepted → running ─┴─ completed
    ↓          ↓           ↓           ↓
  rejected  rejected    failed     archived_accepted
                           │           (via accept_review)
                           └─── blocked
```

**新增状态**：`review_ready`、`blocked`、`archived_accepted`、`archived_rejected`

**新增操作**：

| 操作 | 前置状态 | 后置状态 | 说明 |
|------|---------|---------|------|
| `mark_review_ready` | `running` | `review_ready` | worker 完成任务，请求 review |
| `submit_review` | `review_ready` | `review_ready` | 提交 review 结果（携带 HandoffResult） |
| `accept_review` | `review_ready` | `completed` | reviewer 接受，任务结束 |
| `reject_review` | `review_ready` | `rejected` | reviewer 拒绝，reason 写入 conflict |
| `block_task` | `proposed`/`accepted`/`running` | `blocked` | 遇到 blocker，blocker_ref 记录原因 |
| `release_task` | `blocked` | `blocked_by` 中的前一个状态 | 解锁 blocker |
| `archive_task` | `completed`/`rejected` | `archived_*` | plan closeout 时归档任务 |

### 3.2 reject reason 进入 conflict log

```python
# reject 时自动记录冲突
def reject_review(task_id: str, reviewer_id: str, reason: str) -> TaskTransfer:
    conflict = ConflictRecord(
        task_id=task_id,
        source="reject_reason",
        description=reason,
        involved_agents=[reviewer_id],
    )
    record_conflict(conflict)
    return _transition(task_id, "rejected", expected_status="review_ready", ...)
```

---

## 4. Path Guardrails（文件边界校验）

### 4.1 handoff 时检查 changed files

```python
def _check_path_guardrails(agent: AgentCard, handoff: HandoffResult) -> tuple[bool, list[str]]:
    """
    Returns (allowed, violated_rules).
    For each changed file in handoff.changed_files:
      - If matches forbidden_paths -> violation
      - If not in allowed_paths (when allowed_paths is non-empty) -> potential violation
    """
    violations = []
    for file in handoff.changed_files:
        # Check forbidden
        for pattern in agent.forbidden_paths:
            if _glob_match(pattern, file):
                violations.append(f"Forbidden path: {file} matches {pattern}")
        # Check allowed (if specified)
        if agent.allowed_paths:
            allowed = any(_glob_match(p, file) for p in agent.allowed_paths)
            if not allowed:
                violations.append(f"Changed file {file} not in agent's allowed_paths")
    return len(violations) == 0, violations
```

### 4.2 boundary_review 判决

- 所有 `validation[].blocking=true` 必须 PASS，否则 `boundary_review=block`
- path guardrail 违规时 `boundary_review=block`，`violated_guardrail` 记录违规模式

### 4.3 blocked 状态的 blocker_ref

```python
class TaskTransfer:
    # ... existing fields ...
    blocker_ref: str | None = None  # reason for blocked state
    blocked_at: str | None = None
```

---

## 5. Conflict Board

### 5.1 冲突来源

| 来源 | 触发条件 |
|------|---------|
| `path_violation` | handoff.changed_files 违反 agent allowed/forbidden paths |
| `concurrent_ownership` | 两个 agent claim 了有 shared ownership 的任务 |
| `stale_task` | 任务 TTL 过期（in_progress > 48h） |
| `reject_reason` | reject_review 时 reason 被记录为 conflict |
| `blocker` | 长时间 blocked 的任务 |

### 5.2 Conflict Board 接口

```python
class Registry:
    # ... existing methods ...

    # Plan management
    def create_plan(self, goal: str, created_by: str) -> Plan: ...
    def activate_plan(self, plan_id: str) -> Plan: ...
    def close_plan(self, plan_id: str) -> Plan: ...

    # Dependency
    def list_ready_tasks(self, agent_id: str | None = None) -> list[TaskTransfer]: ...
    def add_dependency(self, task_id: str, depends_on: list[str]) -> None: ...

    # Review lifecycle
    def mark_review_ready(self, task_id: str, agent_id: str, handoff: HandoffResult) -> TaskTransfer: ...
    def submit_review(self, task_id: str, handoff: HandoffResult) -> None: ...
    def accept_review(self, task_id: str, reviewer_id: str) -> TaskTransfer: ...
    def reject_review(self, task_id: str, reviewer_id: str, reason: str) -> TaskTransfer: ...

    # Blocker
    def block_task(self, task_id: str, agent_id: str, reason: str) -> TaskTransfer: ...
    def release_task(self, task_id: str, agent_id: str) -> TaskTransfer: ...

    # Task archive
    def archive_task(self, task_id: str) -> None: ...

    # HandoffResult
    def get_handoff_result(self, task_id: str) -> HandoffResult | None: ...
    def save_handoff_result(self, handoff: HandoffResult) -> None: ...

    # Conflict
    def record_conflict(self, conflict: ConflictRecord) -> None: ...
    def list_conflicts(self, plan_id: str | None = None, resolved: bool | None = None) -> list[ConflictRecord]: ...
    def resolve_conflict(self, conflict_id: str, resolution: str) -> None: ...

    # Packet generation
    def prepare_worker_packet(self, task_id: str, agent_id: str) -> str: ...  # Markdown
    def prepare_review_packet(self, task_id: str) -> str: ...               # Markdown
```

---

## 6. Plan 管理

### 6.1 Plan 生命周期

```
draft → active → completed / cancelled
```

- `create_plan(goal)` → `status=draft`，可编辑
- `activate_plan(plan_id)` → `status=active`，plan 下的任务开始被调度
- `close_plan(plan_id)` → `status=completed`，所有任务归档，未完成的任务标记为 stale

### 6.2 list_ready_tasks 算法

```python
def list_ready_tasks(self, agent_id: str | None = None) -> list[TaskTransfer]:
    # 1. 找出所有 queued 任务
    # 2. 对每个任务计算 blocked_by（基于 depends_on）
    # 3. 检查 blocked_by 是否全部 resolved（completed/cancelled）
    # 4. 如果 agent_id 指定，过滤 capability 匹配
    # 5. 按 priority 排序返回
```

---

## 7. Packet 生成（Markdown）

### 7.1 Worker Packet

```markdown
# Worker Task: {task_id}
## Goal
{context.summary}

## Agent
Assigned to: {agent_id}
Role: {agent.role}
Allowed paths: {agent.allowed_paths}
Forbidden paths: {agent.forbidden_paths}

## Validation
{validation[].command}  # for each validation rule

## Handoff Format
When complete, output:
## Verification
- `<command>`: PASS/FAIL

## Changed Files
- `<file>`

## Docs Touched
- None / <doc>

## Risks
- None / <risk>

Submit with: mac-agent submit-review {task_id} --agent {agent_id}
```

### 7.2 Review Packet

```markdown
# Review Task: {task_id}
## Submitted by
{agent_id} at {timestamp}

## HandoffResult
verification:
{verification[].command}: {verification[].result}

changed_files:
{changed_files}

docs_touched:
{docs_touched}

risks:
{risks}

boundary_review: {boundary_review}
violated_guardrail: {violated_guardrail}

## Validation Results
{validation[].command} [required={required}] [blocking={blocking}]: {result}

## Decision
Accept: mac-agent accept-review {task_id} --reviewer {reviewer_id}
Reject: mac-agent reject-review {task_id} --reviewer {reviewer_id} --reason "<reason>"
```

---

## 8. 数据库 Schema 变更

新增表：

| 表 | 用途 |
|----|------|
| `plans` | Plan 元数据 |
| `plan_tasks` | Plan 与 Task 的关联（plan_id, task_id） |
| `task_dependencies` | depends_on / blocked_by |
| `handoff_results` | HandoffResult JSON |
| `conflict_records` | ConflictRecord JSON |
| `task_validations` | ValidationRule 数组（JSON） |

---

## 9. CLI 新增命令

```bash
# Plan
mac-agent plan create --goal "..."
mac-agent plan activate --plan-id PLAN-001
mac-agent plan close --plan-id PLAN-001
mac-agent plan list

# Task lifecycle
mac-agent next --agent claude           # claim next ready task
mac-agent mark-review-ready --task-id T1 --agent claude
mac-agent submit-review --task-id T1 --agent claude --verification "pytest:pass" --changed-file ...
mac-agent accept-review --task-id T1 --reviewer codex
mac-agent reject-review --task-id T1 --reviewer codex --reason "..."

# Blocker
mac-agent block --task-id T1 --agent claude --reason "needs business decision"
mac-agent release --task-id T1 --agent claude

# Conflict
mac-agent conflicts [--plan-id PLAN-001] [--resolved]
mac-agent resolve-conflict --conflict-id C001 --resolution "fixed by ..."

# Packet
mac-agent worker-packet --task-id T1 --agent claude
mac-agent review-packet --task-id T1

# Ready tasks
mac-agent ready-tasks [--agent claude]
```

---

## 10. 不做的事

- 不做 agent 自动调度（scheduler 的 claim 逻辑保留，但不让 MAC 自动运行外部 CLI）
- 不做 gRPC / Redis / PostgreSQL（Phase B 之后考虑）
- 不做 Multi-workspace（单项目 context）
- 不做 MCP 替代（MAC 不处理资源/工具）

---

*设计文档：MAC Phase A — Plan + Collaboration Layer*
*与 README（用户入口）、CLAUDE.md（AI agent 指南）、SPEC.md（架构规范）同步更新*
