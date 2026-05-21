# MAC Phase A — Minimal Collaboration Layer

> 版本：1.0
> 日期：2026-05-22
> 状态：待批准
> 原则：最小通用化，不绑定项目特定假设，可扩展而非预设

---

## 1. 设计哲学

### 1.1 什么是真正通用的

从 `.agent-state/` 中提取真正通用的协作模式：

| 模式 | 通用性 | 原因 |
|------|--------|------|
| Plan 作为任务分组 | ✅ 通用 | 任何多任务协作都需要分组 |
| depends_on 依赖解锁 | ✅ 通用 | 有依赖的任务需要等待解锁 |
| HandoffResult 交接结构 | ✅ 通用 | worker 间需要结构化交接 |
| ConflictRecord 冲突记录 | ✅ 通用 | 冲突在任何协作中都会发生 |
| Path rules（可配置） | ✅ 通用 | 但规则本身必须可配置，不是硬编码 |

### 1.2 什么应该是可选项

| 功能 | 原因 | 实现方式 |
|------|------|---------|
| Review lifecycle | 不是所有任务都需要 review | 配置开关，默认关闭 |
| Blocker mechanism | 简单场景不需要 | 可选的 extended 字段 |
| Parallel groups | 复杂场景才需要 | 第一版只支持 flat task list |
| Path guardrails | 每个项目不同 | 规则可配置，不预设路径 |

### 1.3 什么不应该进 MAC

- 项目特定的角色定义（`planner_reviewer` 等）
- 硬编码的路径模式（`backend/**` 等）
- PowerShell/CLI 特定行为
- 业务特定的 validation 命令

---

## 2. 数据模型（最小集合）

### 2.1 Plan（简化版）

```python
class Plan(BaseModel):
    plan_id: str
    goal: str
    status: Literal["draft", "active", "completed", "cancelled"] = "draft"
    task_ids: list[str] = Field(default_factory=list)
    created_by: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    closed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

**无 parallel_groups**：第一版只支持 flat task list。依赖通过 `depends_on` 表达。

### 2.2 TaskDependency（轻量）

```python
class TaskDependency(BaseModel):
    task_id: str
    depends_on: list[str] = Field(default_factory=list)  # task_ids this waits for
    preferred_agents: list[str] = Field(default_factory=list)  # soft preference
```

**无 blocking validation**：验证规则放到配置的 policy 中，不硬编码。

### 2.3 HandoffResult（核心结构）

```python
class HandoffResult(BaseModel):
    task_id: str
    plan_id: str | None = None
    agent_id: str
    verification: list[VerificationEntry] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    docs_touched: list[str] = Field(default_factory=list)
    risks: str = ""  # simple string, not list
    boundary_review: Literal["pass", "block", "not_required"] = "not_required"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class VerificationEntry(BaseModel):
    command: str
    result: Literal["pass", "fail"]
    description: str = ""
```

**最小字段**：只保留真正必要的交接信息。

### 2.4 ConflictRecord（轻量）

```python
class ConflictRecord(BaseModel):
    conflict_id: str = Field(default_factory=lambda: str(uuid4()))
    plan_id: str | None = None
    task_id: str | None = None
    source: str  # free-form: "path_violation", "concurrent_ownership", "manual", etc.
    severity: Literal["blocking", "non_blocking"] = "non_blocking"
    description: str
    involved_agents: list[str] = Field(default_factory=list)
    involved_files: list[str] = Field(default_factory=list)
    resolved: bool = False
    resolution: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
```

**source 是字符串**：不预设枚举，支持任何冲突类型。

### 2.5 AgentCard（扩展，最小新增）

```python
class AgentCard(BaseModel):
    agent_id: str
    name: str
    capabilities: list[AgentCapability] = Field(default_factory=list)
    # --- 路径权限（可选项，默认空） ---
    allowed_paths: list[str] = Field(default_factory=list)   # empty = no restriction
    forbidden_paths: list[str] = Field(default_factory=list)  # empty = no restriction
    # --- 其他已有字段 ---
    load: int = Field(default=0, ge=0, le=100)
    status: str = "online"
    metadata: dict[str, Any] = Field(default_factory=dict)
```

**空 = 无限制**：不预设任何路径规则。

### 2.6 TaskTransfer（扩展）

```python
class TaskTransfer(BaseModel):
    # ... existing fields ...
    depends_on: list[str] = Field(default_factory=list)  # NEW: task dependencies
    plan_id: str | None = None  # NEW: which plan this task belongs to

    # Review lifecycle (only used when policy enables it)
    review_ready_at: str | None = None
    review_decision: Literal["accepted", "rejected"] | None = None
    review_decided_by: str | None = None
    review_decided_at: str | None = None
    review_reject_reason: str | None = None

    # Handoff
    handoff_result: HandoffResult | None = None
```

---

## 3. Path Guardrails（可配置规则）

### 3.1 PathRule（项目级配置）

```python
class PathRule(BaseModel):
    """Project-level path rules for coordination.

    These rules are NOT hard-coded. They are configured per-project
    via a rules file or environment.
    """

    allow_all: bool = True  # if True, path checking is disabled
    forbidden_patterns: list[str] = Field(default_factory=list)  # e.g. ["db/**", "fixtures/gold/**"]
    allowed_patterns: list[str] = Field(default_factory=list)    # e.g. ["backend/**", "tests/**"]

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> PathRule:
        """Load from project config or environment.

        Example config:
        {
            "MAC_PATH_RULES": "backend/**,tests/**|db/**,fixtures/gold/**"
            # format: "allowed1,allowed2|forbidden1,forbidden2"
        }
        """
        return cls()
```

### 3.2 校验逻辑

```python
def check_path_guardrails(
    agent: AgentCard,
    handoff: HandoffResult,
    rules: PathRule,
) -> tuple[bool, list[str]]:
    """Return (allowed, violations). Empty rules = no checking."""
    if rules.allow_all and not rules.forbidden_patterns and not rules.allowed_patterns:
        return True, []

    violations = []
    for file in handoff.changed_files:
        # Check forbidden
        for pattern in rules.forbidden_patterns:
            if _glob_match(pattern, file):
                violations.append(f"Forbidden: {file} matches {pattern}")
        # Check allowed (only if specified)
        if rules.allowed_patterns:
            if not any(_glob_match(p, file) for p in rules.allowed_patterns):
                violations.append(f"Not allowed: {file} not in {rules.allowed_patterns}")

    return len(violations) == 0, violations
```

**关键**：规则通过配置传入，不是硬编码。MAC 本身不知道 `backend/**` 或 `db/**` 是什么。

---

## 4. Review Lifecycle（可配置开关）

### 4.1 Policy 配置

```python
class CoordinationPolicy(BaseModel):
    """Project-level policy for how MAC coordinates agents.

    This controls which features are enabled and how they behave.
    """

    require_review: bool = False  # Default: tasks complete without review
    require_path_check: bool = False  # Default: no path checking
    path_rules: PathRule = Field(default_factory=PathRule)
    allow_self_assign: bool = True  # Can agent claim own task?
    max_retry_count: int = 3

    @classmethod
    def from_env(cls) -> CoordinationPolicy:
        """Load from environment or project config."""
        return cls()
```

### 4.2 Review 操作（仅在 require_review=True 时激活）

| 操作 | 触发条件 | 结果 |
|------|---------|------|
| `mark_review_ready` | `require_review=True` 时，`complete` 变为先进入 review_ready | 任务等待 accept/reject |
| `accept_review` | reviewer 批准 | 任务完成 |
| `reject_review` | reviewer 拒绝 | reason 写入 conflict，任务标记 rejected |

**当 `require_review=False`**：任务直接 `complete`，不走 review 流程。

---

## 5. 核心 API（最小集）

### 5.1 Plan 管理

```python
class Registry:
    def create_plan(self, goal: str, created_by: str) -> Plan: ...
    def activate_plan(self, plan_id: str) -> Plan: ...
    def close_plan(self, plan_id: str) -> Plan: ...
    def list_plans(self, status: str | None = None) -> list[Plan]: ...
```

### 5.2 依赖解锁

```python
    def list_ready_tasks(
        self,
        agent_id: str | None = None,
        capability: str | None = None,
    ) -> list[TaskTransfer]:
        """
        Returns tasks that are:
        - status = 'proposed' or 'queued'
        - all depends_on tasks are completed/accepted/cancelled
        - agent matches capability (if specified)
        - ordered by priority
        """

    def submit_task(self, task: TaskTransfer, depends_on: list[str] | None = None) -> TaskTransfer:
        """Submit task with optional dependency."""
```

### 5.3 Handoff + Review

```python
    def save_handoff_result(self, handoff: HandoffResult) -> None: ...
    def get_handoff_result(self, task_id: str) -> HandoffResult | None: ...

    def mark_review_ready(self, task_id: str, agent_id: str, handoff: HandoffResult) -> TaskTransfer:
        """Move task to review_ready state (only if policy.require_review=True)."""

    def accept_review(self, task_id: str, reviewer_id: str) -> TaskTransfer: ...
    def reject_review(self, task_id: str, reviewer_id: str, reason: str) -> TaskTransfer:
        """Reject. Reason is recorded as a conflict."""

    def complete_task(self, task_id: str, agent_id: str) -> TaskTransfer:
        """
        If require_review=False: directly complete.
        If require_review=True: move to review_ready instead.
        """
```

### 5.4 Conflict

```python
    def record_conflict(self, conflict: ConflictRecord) -> None: ...
    def list_conflicts(
        self,
        plan_id: str | None = None,
        resolved: bool | None = None,
    ) -> list[ConflictRecord]: ...
    def resolve_conflict(self, conflict_id: str, resolution: str) -> None: ...
```

### 5.5 Packet（简单输出）

```python
    def prepare_worker_packet(self, task_id: str) -> str:
        """Generate a simple worker prompt in Markdown format."""

    def prepare_review_packet(self, task_id: str) -> str:
        """Generate a simple review prompt in Markdown format."""
```

---

## 6. 状态机（最终形态）

### 6.1 当 `require_review=False`（默认）

```
proposed → accepted → running → completed
    ↓          ↓           ↓
  rejected   rejected   failed
              ↓
          cancelled
```

### 6.2 当 `require_review=True`

```
proposed → accepted → running → review_ready → completed
    ↓          ↓           ↓           ↓
  rejected   rejected   failed      rejected
              ↓                    (reason → conflict)
          cancelled
```

---

## 7. 不做的事

| 不做 | 原因 |
|------|------|
| 无 parallel_groups | 第一版只支持 flat task list，依赖用 depends_on |
| 无 blocker_ref | 简单场景不需要；复杂场景通过 conflict 处理 |
| 无 path rules 硬编码 | 规则通过配置传入，不预设任何路径 |
| 无 agent roles 硬编码 | 角色是项目特定的，MAC 只管 capability |
| 无 automatic scheduler | 第一版只做 claim，不自动调度 |
| 无 gRPC/Redis/Cloud | 这些是 Phase B 之后的事 |

---

## 8. 第一版实现范围（可操作子集）

Phase A 分两批实现：

**Batch 1（核心，可独立验证）**：
1. Plan model + create/activate/close
2. `depends_on` on TaskTransfer + `list_ready_tasks`
3. `HandoffResult` model + save/get
4. ConflictRecord + record/list/resolve
5. PathRule 配置 + 校验（简单版）
6. `prepare_worker_packet` / `prepare_review_packet`

**Batch 2（可选，视复杂度决定）**：
1. `require_review` policy + `mark_review_ready` / `accept_review` / `reject_review`
2. Policy 从环境变量加载

---

*设计文档：MAC Phase A (Minimal) — 轻量协作层*