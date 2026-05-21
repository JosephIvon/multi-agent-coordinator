# Multi-Agent Coordinator (MAC) 设计方案

> 版本：1.5
> 日期：2026-05-22
> 状态：已批准

---

## 1. 定位与目标

**项目名称**：multi-agent-coordinator（MAC）

**定位**：通用跨 Agent 任务交接与协调层，支撑"任务交接、上下文交接、验证交接协议、运行账本"四个核心能力。

**目标用户**：
- 开发者：通过本地 API 或 CLI bridge 接入任何 Python 项目
- AI Agent（Claude Code、Trae、智谱、Hermes 等）：通过 A2A-compatible task profile 协作
- CI/CD 系统：将构建、测试、验证结果作为可审计任务写入 MAC 账本

**非目标**：
- 不做 MCP 的替代品；MCP 继续负责资源、工具、上下文引用
- 不做 LangGraph/CrewAI 的替代品；MAC 是轻量跨 Agent 任务交接协议层
- 不做 gRPC、Redis、Postgres、Cloud Bridge（Phase 2）

---

## 2. 核心概念

### 2.1 AgentCard

```python
class AgentCard(BaseModel):
    agent_id: str
    name: str
    version: str = "1.0"
    capabilities: list[AgentCapability]
    load: int = Field(default=0, ge=0, le=100)
    status: str = "online"
    last_heartbeat: float = 0
    project_context: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 2.2 ContextBundle

```python
class ContextBundle(BaseModel):
    summary: str
    artifact_refs: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    decision_log: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 2.3 TaskTransfer

```python
class TaskTransfer(BaseModel):
    task_id: str
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    source_agent_id: str | None = None
    target_agent_id: str | None = None
    payload: TaskPayload | None = None
    context: ContextBundle | None = None
    test_contract: TestContract | None = None
    priority: int = Field(default=5, ge=1, le=10)
    status: str = "proposed"
    max_hops: int = Field(default=5, ge=1)
    current_hops: int = Field(default=0, ge=0)
    ttl_seconds: int = Field(default=3600, ge=1)
    error_code: str | None = None
    retry_count: int = Field(default=0, ge=0)
    fallback_agent_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 2.4 TestContract

```python
class TestContract(BaseModel):
    risk_level: RiskLevel  # low | medium | high
    recommended_commands: list[str] = Field(default_factory=list)
    required_commands: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    allow_manual_override: bool = False

    @classmethod
    def for_risk(cls, risk_level: RiskLevel) -> TestContract:
        # low: pytest smoke + test_output
        # medium: pytest tests + test_output, changed_files
        # high: pytest --cov + test_output, coverage_report, review_notes
```

### 2.5 AuditEntry

```python
class AuditEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str
    task_id: str
    agent_id: str = ""
    actor: str = ""
    action: str
    from_status: str | None = None
    to_status: str | None = None
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
```

---

## 3. 状态机

```
proposed → accepted → running → completed
    ↓          ↓           ↓
  rejected   rejected    failed
              ↓
          cancelled (via cancel_task)
```

状态转换规则：
- `proposed → accepted`：目标 Agent 显式 accept，或通过 `claim_next_task()` CAS 更新
- `accepted → running`：Agent 调用 `start_task()`
- `running → completed`：`complete_task()` 时 quality gate 通过
- `running → failed`：执行异常、TTL 过期、max_hops 超限或 quality gate 失败
- 任意状态 → `cancelled`：显式调用 `cancel_task()`

---

## 4. 传输层

### 4.1 进程内 API

直接使用 `Registry` 类：

```python
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage

registry = Registry(SQLiteStorage("mac.db"))
registry.register(agent)
registry.submit_task(task)
```

### 4.2 HTTP 适配器（FastAPI）

```python
from mac.transport.http_ws import create_app

app = create_app(Registry(SQLiteStorage("mac.db")))
# uvicorn app:app --port 8000
```

端点见 `README.md` 端点表。

---

## 5. SQLite Task Ledger

表结构：

| 表 | 用途 |
|----|------|
| `agent_cards` | Agent 注册信息、capability、load、status |
| `task_transfers` | 任务状态、payload、project_context |
| `audit_entries` | 审计事件（按 task_id 索引） |
| `quality_results` | 质量证据（按 task_id + retry_count 索引） |
| `agent_outcomes` | Agent 执行结果（按 agent_id + capability 聚合） |

WAL 模式启用，支持单实例高并发读写。

---

## 6. 错误码

| 错误码 | 含义 |
|--------|------|
| `StateConflictError` | 状态转换冲突（CAS 失败） |
| `QualityGateError` | quality gate 未通过 |
| `TaskExpiredError` | TTL 过期 |
| `MaxHopsExceededError` | 超过最大跳数 |
| `StatusConflict` | SQLite 层 CAS 失败（storage 内） |

---

## 7. Phase 状态

| Phase | 内容 | 状态 |
|-------|------|------|
| 1.0–1.8 | MVP 完成 | ✅ |
| 1.9 | Failure recovery (checkpoint/retry/cancel) + TaskEventBus | ✅ |
| 2 | gRPC、Redis、PostgreSQL、Cloud Bridge | 延期 |

---

## 8. 已知约束

- SQLite WAL 单实例；多实例强一致性延期至 Phase 2 PostgreSQL
- ContextBundle 质量决定 handoff 质量；MAC 强制结构，不强制理解
- Quality Gate 检查证据是否满足合同，不评判测试本身质量
- Observed capability metrics 是观察值，不是认证或 SLA

---

*设计文档 v1.5，与 README（用户入口）、CLAUDE.md（AI agent 指南）同步更新。*