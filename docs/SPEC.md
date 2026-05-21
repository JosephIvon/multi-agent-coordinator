# Multi-Agent Coordinator (MAC) 设计方案

> 版本：1.14
> 日期：2026-05-22
> 状态：第一阶段 release hardening + 综合改进：修复 Windows 路径风险信号误匹配、移除未使用 quality_gate 和 expected_status 字段、统一模型层（删除 storage fallback）、HTTP 补全 heartbeat 和 agent 查询端点、CLI status 改为经由 Registry、claim 端点 404 行为一致性、health check 端点、SQLite audit 查询性能优化

---

## 1. 定位与目标

**项目名称**：multi-agent-coordinator（MAC）

**定位**：通用跨 Agent 任务交接与协调层，支撑“任务交接、上下文交接、验证交接协议、运行账本”四个核心能力。

**目标用户**：
- 开发者：任何 Python 项目通过本地 API 或 CLI bridge 接入
- AI Agent（Claude Code、Trae、智谱、Hermes 等）：通过 A2A-compatible task profile 协作
- CI/CD 系统：将构建、测试、验证结果作为可审计任务写入 MAC 账本

**非目标**：
- 不做 MCP 的替代品；MCP 继续负责资源、工具、上下文引用，MAC 负责调度、账本、handoff 和 quality gate
- 不做 LangGraph/CrewAI 的替代品；MAC 是轻量的跨 Agent 任务交接协议层
- 第一阶段不做 gRPC、Redis、Postgres、Cloud Bridge、Hybrid Bridge，避免 MVP 同时承担传输、存储和云端部署复杂度

---

## 2. 第一阶段 MVP 范围

第一阶段目标是做出一个可在单机/单项目内跑通的 MAC 核心闭环：任务进入、Agent 发现、上下文交接、状态流转、验证合同、账本审计、CLI 接入。Phase 1.1 在这个闭环上补充可观测的能力指标、自动测试合同规划和最小 HTTP 接入层。Phase 1.2 在同一套本地账本模型内补充 capability-based task claiming，让在线 Agent 可以主动 claim 匹配能力的 proposed task。Phase 1.3 在 claim 之上补充本地 Agent Adapter Loop，让一个 adapter 可以注册、认领一条任务、执行显式配置的 handler 或命令、提交质量证据、完成或失败任务，并记录 observed outcome。Phase 1.4 在 runner 之上补充可复用 adapter template，复用 agent 身份、capability、project_context、metadata 和 handler 配置。Phase 1.5 补充只读 task visibility，让 agent 和人类操作者可以按 status、capability、agent assignment 和 project_context 查看账本任务。Phase 1.6 补充只读 task evidence bundle，把 task snapshot、quality results、audit trail 和 observed capability score 聚合成可交接视图。Phase 1.7 补充只读 quality gate preview，让调用方在 complete 前看到当前证据是否满足 TestContract 以及缺口明细。Phase 1.8 补充只读 task readiness / next action preview，让 agent 和人类操作者在不改变状态的前提下看到下一步建议及阻塞原因；它仍保持本地、单机、SQLite 优先的 MVP 边界。

### 2.1 MVP 必须包含

| 能力 | 第一阶段实现 |
|------|--------------|
| 协议入口 | HTTP/WebSocket 或进程内 API，两者共享同一套领域模型 |
| 运行账本 | SQLite Task Ledger，启用 WAL，记录 task、handoff、audit、quality gate |
| 上下文交接 | ContextBundle，任务只携带摘要、引用和约束，不塞入大段原始内容 |
| 验证交接 | Risk-Based TestContract，按风险决定必须运行的验证命令和质量门槛 |
| Agent 接入 | AgentCard + A2A-compatible task profile |
| CLI 接入 | CLI bridge，用于 register、submit、status、handoff、complete、fail、ledger 查询 |
| 状态控制 | TTL、max_hops、CAS 乐观锁、标准错误码 |
| Phase 1.1 能力指标 | observed capability metrics，从任务结果、quality evidence 和审计事件中累计观察值 |
| Phase 1.1 合同规划 | automatic TestContract planner，根据任务类型、风险和 ContextBundle 生成最小验证合同 |
| Phase 1.1 HTTP 接入 | minimal FastAPI HTTP adapter，暴露本地 Registry service 的核心操作 |
| Phase 1.2 任务认领 | capability-based task claiming，Agent 可按声明能力主动 claim proposed task，成功后 accepted |
| Phase 1.3 Adapter Loop | LocalAgentRunner 单次执行闭环：register -> claim -> start -> handler/command -> quality -> complete/fail -> observe outcome |
| Phase 1.4 Adapter Templates | LocalAgentTemplate / command_agent_template / pytest_agent_template 复用本地 adapter 配置并生成一次性 runner |
| Phase 1.5 Task Visibility | 只读任务查询，按 status、capability、agent assignment 和 project_context 过滤任务 |
| Phase 1.6 Task Evidence Bundle | 只读证据包视图，聚合 task、quality results、audit trail 和 observed capability score |
| Phase 1.7 Quality Gate Preview | 只读预览当前 quality results 是否满足 TestContract，并列出缺失 command/evidence |
| Phase 1.8 Task Readiness / Next Action Preview | 只读预览任务当前推荐下一步、执行 agent、required capability 和阻塞原因 |
| Phase 1 release hardening | canonical `source_agent_id` / `target_agent_id`、discover/claim observed ranking、failure recovery、并发 claim/quality 回归、进程内 TaskEventBus |

### 2.2 Phase 2 延后项

| 能力 | 延后原因 |
|------|----------|
| gRPC + Protobuf | 第一阶段 payload 应引用化，先不优化大体量传输 |
| Redis Pub/Sub | 先用 SQLite + 本地事件分发验证协议语义 |
| PostgreSQL | 单机账本足够支撑 MVP，生产级多实例一致性放到后续 |
| Cloud Bridge | 先收敛本地和进程内协作，跨网络、认证、租户隔离后置 |
| Hybrid Bridge | 依赖 Cloud Bridge 的成熟，不进入 MVP |
| 自动能力认证 | Phase 1.1 只记录 observed metrics，不把观察值升级成 verified capability 或认证体系 |

---

## 3. A2A / MCP / MAC 边界

### 3.1 A2A-compatible task profile

MAC 的任务模型保持 A2A-compatible：任务有明确的 `task_id`、`trace_id`、状态、输入摘要、期望输出和执行方信息。MAC 不强行复制外部协议的完整对象模型，而是提供一个可映射到 A2A 的任务 profile，保证 Agent 之间可以用稳定结构交接。

### 3.2 MCP 的职责

MCP 用于资源、工具和上下文引用：
- `mcp_uri` 指向文件、文档、日志、测试报告、工具结果
- Agent 按需通过 MCP 或文件系统读取上下文
- MAC 不直接托管大段代码、日志或二进制内容

### 3.3 MAC 的职责

MAC 管四件事：
- **调度**：基于能力、负载、项目亲和性选择候选 Agent
- **账本**：用 SQLite Task Ledger 记录状态流转和审计轨迹
- **handoff**：定义任务、上下文、验证合同和责任边界如何交接
- **quality gate**：根据 TestContract 判断任务是否可以完成
- **能力观察**：Phase 1.1 记录 observed capability metrics，辅助后续调度，但不替代 AgentCard 的显式能力声明

一句话边界：MCP 告诉 Agent “资源和工具在哪里”，A2A-compatible profile 告诉 Agent “任务长什么样”，MAC 决定“谁接、怎么交、如何记账、何时算完成”。

---

## 4. 核心概念

### 4.1 AgentCard（能力名片）

```python
class AgentCapability(BaseModel):
    name: str
    proficiency: str = "intermediate"  # beginner | intermediate | advanced
    frameworks: list[str] = []
    context_window: int | None = None
    max_payload_size: int = 64 * 1024


class AgentCard(BaseModel):
    agent_id: str
    name: str
    version: str = "1.0"
    capabilities: list[AgentCapability]
    endpoint: str | None = None        # HTTP/WS 地址；进程内 Agent 可为空
    load: int = 0
    status: str = "online"             # online | busy | offline
    last_heartbeat: float = 0
    project_context: str | None = None
    metadata: dict[str, Any] = {}
```

Phase 1.1 增加观察型能力指标。它们来自 MAC 已经能看到的任务状态、quality gate 结果和审计日志，用于排序和诊断；它们不是外部认证，也不自动授予 Agent 新能力。

```python
class CapabilityMetric(BaseModel):
    agent_id: str
    capability: str
    task_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    quality_gate_pass_count: int = 0
    quality_gate_fail_count: int = 0
    average_duration_ms: float | None = None
    last_observed_at: str = ""
    metadata: dict[str, Any] = {}
```

### 4.2 ContextBundle（上下文交接包）

ContextBundle 是第一阶段的关键收敛点：任务必须带足摘要和引用，让接收方能重建工作现场；同时禁止把不可控的大段内容直接塞进 payload。

```python
class ContextRef(BaseModel):
    kind: str                           # mcp | file | url | command_output
    uri: str                            # mcp://..., file://..., https://...
    description: str = ""
    required: bool = True


class ContextBundle(BaseModel):
    summary: str
    current_state: str = ""
    refs: list[ContextRef] = []
    constraints: list[str] = []
    assumptions: list[str] = []
    open_questions: list[str] = []
```

### 4.3 Risk-Based TestContract（验证合同）

TestContract 把“完成任务前必须验证什么”结构化。风险越高，验证要求越强；MAC 负责记录和检查合同，不替代实际测试框架。

```python
class TestContract(BaseModel):
    risk_level: str                     # low | medium | high
    required_commands: list[str] = []
    required_evidence: list[str] = []   # coverage, lint, e2e, manual_review
    allow_manual_override: bool = False
```

建议默认策略：

| 风险 | 必须验证 |
|------|----------|
| low | 精准单测或静态检查至少一项 |
| medium | 相关单测 + 关键集成路径 |
| high | 相关单测 + 集成测试 + 质量证据写入账本 |

Phase 1.1 增加 automatic TestContract planner。planner 只生成初始合同，不执行测试，也不绕过 quality gate；调用方仍可以在提交任务时覆盖或收紧合同。

```python
class TestContractPlanner(ABC):
    async def plan(self, task: TaskTransfer) -> TestContract:
        """
        根据 task_type、risk_level、ContextBundle refs/constraints 和项目默认策略，
        生成 required_commands 和 required_evidence。
        """
```

### 4.4 TaskTransfer（任务交接）

```python
class TaskTransfer(BaseModel):
    task_id: str
    trace_id: str
    source_agent_id: str | None = None
    target_agent_id: str | None = None
    task_type: str                      # code_review | write_test | validate_tests | write_code | custom
    context: ContextBundle
    test_contract: TestContract | None = None
    priority: int = 5
    status: str = "proposed"            # proposed | accepted | running | completed | failed | rejected
    max_hops: int = 5
    current_hops: int = 0
    ttl_seconds: int = 3600
    error_code: str | None = None
    retry_count: int = 0
    fallback_agent_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = {}
```

### 4.5 AuditEntry（结构化审计日志）

```python
class AuditEntry(BaseModel):
    trace_id: str
    task_id: str
    agent_id: str
    action: str                         # register | heartbeat | propose | claim_task | accept | reject | update | complete | fail | gate_pass | gate_fail
    from_status: str | None = None
    to_status: str | None = None
    message: str = ""
    timestamp: str = ""
    metadata: dict[str, Any] = {}
```

### 4.6 TaskEvidenceBundle（任务证据包）

Phase 1.6 增加只读证据包模型，用于把一次任务交接和执行后的核心证据聚合到同一个响应中：

```python
class TaskEvidenceBundle(BaseModel):
    task_id: str
    trace_id: str
    task: TaskTransfer
    quality_results: list[dict[str, Any]] = []
    audit_trail: list[AuditEntry] = []
    execution_agent_id: str | None = None
    required_capability: str | None = None
    observed_capability_score: dict[str, Any] | None = None
```

`observed_capability_score` 来自 observed capability metrics；它是诊断信息，不是授权、认证或 quality gate 的替代。

### 4.7 QualityGatePreview（质量门预览）

Phase 1.7 增加只读 quality gate 预览模型，用于在调用 `complete_task()` 前看到当前质量证据是否满足 TestContract：

```python
class QualityGatePreview(BaseModel):
    task_id: str
    trace_id: str
    has_contract: bool
    allowed: bool
    reason: str | None = None
    required_commands: list[str] = []
    required_evidence: list[str] = []
    passed_commands: list[str] = []
    present_evidence: list[str] = []
    missing_commands: list[str] = []
    missing_evidence: list[str] = []
    quality_results_count: int = 0
```

preview 使用和 `complete_task()` 相同的 `evaluate_quality_gate()` 规则，但只读返回判断和缺口明细，不改变任务状态。

### 4.8 TaskReadinessReport（任务就绪度 / 下一步建议）

Phase 1.8 增加只读 readiness 报告模型，用于在不改变任务状态的前提下告诉 agent 或人类操作者“下一步应该调用哪个显式操作”：

```python
class TaskReadinessReport(BaseModel):
    task_id: str
    trace_id: str
    status: str
    execution_agent_id: str | None = None
    required_capability: str | None = None
    next_action: str
    blocking_reason: str | None = None
    quality_allowed: bool | None = None
    missing_commands: list[str] = []
    missing_evidence: list[str] = []
    quality_results_count: int = 0
    audit_event_count: int = 0
```

`next_action` 使用现有协议动作词汇，例如 `claim_task`、`accept_handoff`、`start_task`、`submit_quality_result`、`complete_task`、`inspect_failure` 或 `none`。`quality_allowed` 只表示 quality gate 当前判断，不等同于执行权或调度授权。

---

## 5. 传输层设计

### 5.1 第一阶段模式

| 模式 | 协议 | 适用场景 |
|------|------|----------|
| In-Process Mode | Python API | 同一进程内多个 Agent 或测试场景 |
| Local Service Mode | HTTP + WebSocket | 单机器多 Agent、CLI bridge、长期运行 Registry |

HTTP/WebSocket 和进程内 API 共享同一套 service 层和 SQLite 账本，不分裂业务逻辑。

Phase 1.1 的 HTTP 接入先采用 minimal FastAPI HTTP adapter：它只把 HTTP 请求映射到现有 Registry service，不引入新的业务状态，也不实现 Cloud Bridge、gRPC gateway、Redis fanout 或多实例部署。WebSocket 仍可作为后续本地事件推送能力，不是 Phase 1.1 的完成条件。

Phase 1.2 的 task claiming 仍是普通请求/响应式 service 操作：Agent 按 capability 主动请求下一条可认领的 proposed task，MAC 在 SQLite 账本内完成校验和 CAS 状态更新。它不是长轮询、WebSocket streaming、Redis queue、Postgres advisory lock、gRPC stream 或 Cloud Bridge。

Phase 1.3 的 Agent Adapter Loop 继续运行在本地进程内：runner 只在调用 `run_once()` 时执行一次 register/claim/start/finish 闭环，不持有后台 daemon，不订阅 streaming 日志，也不引入 Redis/Postgres/gRPC/Cloud Bridge。命令型 adapter 只能执行调用方在 adapter 配置中传入的 command vector；task payload 可以描述任务意图、上下文和验证期望，但不能成为 shell 命令来源。

Phase 1.4 的 Agent Adapter Templates 只封装本地 adapter 配置：`LocalAgentTemplate` 保存 agent 身份、单一 capability、可选 project_context/metadata 和 handler；`command_agent_template()` 与 `pytest_agent_template()` 生成常见验证 adapter。template 只负责创建 `LocalAgentRunner`，不自己执行任务、不轮询、不常驻、不加载插件、不从 task payload 读取可执行命令。

Phase 1.5 的 Task Visibility 是只读查询能力：调用方可以按 `status`、required `capability`、可认领 `agent_id` 和 `project_context` 查看账本中的任务。该能力不改变任务状态、不写 audit、不提供长轮询或 lease；真正取得执行权仍必须调用 `claim_task` 或显式 `accept_handoff`。

Phase 1.6 的 Task Evidence Bundle 是只读聚合视图：调用方按 `task_id` 获取 task snapshot、quality results、audit trail、execution agent、required capability 和 observed capability score。它用于交接复盘、调试和人类 review，不重新判断 quality gate，不写入新 audit，也不改变任何 task 状态。

Phase 1.7 的 Quality Gate Preview 是只读质量门预检：调用方按 `task_id` 获取当前 quality results 对 TestContract 的满足情况、阻塞原因、缺失 command 和缺失 evidence。它不 complete、不 fail、不写 audit，也不替代最终的 `complete_task()` 检查。

Phase 1.8 的 Task Readiness / Next Action Preview 是只读下一步建议：调用方按 `task_id` 获取当前任务状态、执行 agent、required capability、推荐下一步和阻塞原因。它不 claim、不 accept、不 start、不 complete、不 fail、不写 audit，也不替代 `claim_task` 的执行权获取或 `complete_task()` 的最终 quality gate。

最小端点：
- `POST /agents/register`
- `POST /agents/heartbeat`
- `GET /agents?capability=...`
- `GET /tasks?status=...&capability=...&agent_id=...&project_context=...`
- `POST /agents/{agent_id}/claim`
- `POST /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/evidence`
- `GET /tasks/{task_id}/quality-preview`
- `GET /tasks/{task_id}/readiness`
- `POST /tasks/{task_id}/accept`
- `POST /tasks/{task_id}/reject`
- `POST /tasks/{task_id}/quality-results`
- `POST /tasks/{task_id}/complete`
- `POST /tasks/{task_id}/fail`
- `GET /ledger/audit/{trace_id}`

### 5.2 传输层抽象

```python
class Transport(ABC):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, target_agent_id: str, message: dict[str, Any]) -> None: ...
    async def broadcast(self, message: dict[str, Any]) -> None: ...
```

第一阶段不实现 `relay_to_cloud`，不根据 payload 大小自动切换 gRPC。payload 超过 Agent 的 `max_payload_size` 时返回 `PAYLOAD_TOO_LARGE`，调用方应改用 ContextBundle 引用。

---

## 6. SQLite Task Ledger

### 6.1 账本职责

SQLite Task Ledger 是第一阶段的事实来源：
- 保存 AgentCard 和 heartbeat 状态
- 保存 TaskTransfer 当前状态
- 追加 handoff 和 audit 事件
- 保存 TestContract 和验证结果
- 保存 Phase 1.1 observed capability metrics
- 支持按 `task_id`、`trace_id` 查询完整轨迹

### 6.2 存储抽象

```python
class TaskLedger(ABC):
    async def save_agent_card(self, card: AgentCard) -> None: ...
    async def list_agents(self, capability: str | None = None) -> list[AgentCard]: ...
    async def create_task(self, task: TaskTransfer) -> None: ...
    async def get_task(self, task_id: str) -> TaskTransfer | None: ...
    async def claim_next_task(self, agent_id: str, capability: str, project_context: str | None = None) -> TaskTransfer | None: ...
    async def update_task_status(self, task_id: str, status: str, expected_status: str | None = None) -> bool: ...
    async def append_audit(self, entry: AuditEntry) -> None: ...
    async def get_audit_trail(self, trace_id: str) -> list[AuditEntry]: ...
    async def record_quality_result(self, task_id: str, result: dict[str, Any]) -> None: ...
    async def update_capability_metrics(self, task_id: str) -> None: ...
    async def get_capability_metrics(self, agent_id: str) -> list[CapabilityMetric]: ...
```

### 6.3 默认实现

```python
class SQLiteTaskLedger(TaskLedger):
    def __init__(self, db_path: str = "mac.db"):
        self.db_path = db_path
        # PRAGMA journal_mode=WAL
```

建议表：
- `agents`
- `tasks`
- `audit_log`
- `quality_results`
- `capability_metrics`

---

## 7. 协议操作

| 操作 | 方向 | 说明 |
|------|------|------|
| `register` | Agent -> MAC | 上线并写入 AgentCard |
| `heartbeat` | Agent -> MAC | 刷新状态和负载 |
| `discover` | Agent/CLI -> MAC | 按能力和负载查找候选 Agent |
| `submit_task` | Agent/CLI -> MAC | 创建任务并写入账本 |
| `list_tasks` | Agent/CLI -> MAC | Phase 1.5 只读列出任务，支持 status、capability、agent assignment 和 project_context 过滤 |
| `get_task_evidence` | Agent/CLI -> MAC | Phase 1.6 只读获取任务证据包，聚合 task、quality、audit 和 observed score |
| `preview_quality_gate` | Agent/CLI -> MAC | Phase 1.7 只读预览当前 quality results 是否满足 TestContract |
| `preview_task_readiness` | Agent/CLI -> MAC | Phase 1.8 只读预览任务当前推荐下一步和阻塞原因 |
| `plan_test_contract` | Agent/CLI/MAC -> MAC | Phase 1.1 根据任务上下文生成初始 TestContract |
| `propose_handoff` | MAC -> Agent | 请求目标 Agent 接收任务 |
| `claim_task` | Agent -> MAC | Phase 1.2 Agent 按能力主动认领 proposed task；尊重 `target_agent_id`，CAS 成功后状态变为 accepted，并将认领方固化为 `target_agent_id` |
| `accept_handoff` | Agent -> MAC | 接收任务，状态变为 accepted |
| `reject_handoff` | Agent -> MAC | 拒绝任务，记录原因 |
| `task_update` | Agent -> MAC | 更新执行进度或状态 |
| `submit_quality_result` | Agent/CI -> MAC | 写入验证结果 |
| `complete_task` | Agent -> MAC | quality gate 通过后完成 |
| `fail_task` | Agent/MAC -> MAC | 失败并记录错误码 |
| `get_capability_metrics` | Agent/CLI -> MAC | 查询 observed capability metrics |

---

## 8. 状态机

```
proposed -> accepted -> running -> completed
    |          |           |
 rejected   rejected    failed
    |
 failed
```

状态规则：
- `proposed -> accepted`：接收方显式 accept，或 Phase 1.2 中符合条件的 Agent 主动 claim
- `proposed -> rejected`：接收方显式 reject
- `accepted -> running`：接收方开始执行
- `running -> completed`：TestContract 为空或 quality gate 通过
- `running -> failed`：执行异常、TTL 到期、max_hops 超限或 quality gate 失败
- 所有状态更新支持 `expected_status`，用于 CAS 乐观锁；claim 必须使用 `expected_status="proposed"`，避免多个 Agent 同时认领同一任务

---

## 9. 核心算法

### 9.1 能力发现 + 负载均衡

```python
async def discover(capability: str, exclude_agent: str | None = None) -> list[AgentCard]:
    """
    1. 过滤：capability 匹配、status=online、load < 80、不在 exclude
    2. 排序：同 project_context 优先，其次 observed capability metrics 更好者优先，再次 load 低者优先
    3. 返回候选列表，由调用方或 MAC service 选择目标
    """
```

### 9.2 TTL 和最大跳数

```python
def validate_task_transfer(task: TaskTransfer) -> tuple[bool, str | None]:
    if task.current_hops >= task.max_hops:
        return False, "MAX_HOPS_EXCEEDED"
    if is_expired(task.created_at, task.ttl_seconds):
        return False, "TTL_EXPIRED"
    return True, None
```

### 9.3 Quality Gate

```python
def evaluate_quality_gate(contract: TestContract | None, results: list[dict[str, Any]]) -> tuple[bool, str | None]:
    """
    contract 为空时允许完成。
    contract 存在时，必须能在 results 中找到 required_commands 和 required_evidence 的通过记录。
    high 风险任务必须记录至少一条可追溯的验证结果。
    """
```

### 9.4 Observed Capability Metrics

```python
def update_observed_capability_metrics(task: TaskTransfer, quality_results: list[dict[str, Any]]) -> None:
    """
    只基于已落账的事实更新统计：completed/failed、quality gate pass/fail、持续时间和任务类型。
    指标用于发现排序和诊断，不改变 AgentCard.capabilities 的声明内容。
    """
```

### 9.5 Automatic TestContract Planner

```python
def plan_test_contract(task_type: str, risk_level: str, context: ContextBundle) -> TestContract:
    """
    从任务类型、风险等级、上下文约束和项目默认策略生成最小 TestContract。
    planner 产出的合同必须仍经过 evaluate_quality_gate 检查。
    """
```

### 9.6 Capability-Based Task Claiming（Phase 1.2）

```python
def claim_task(task: TaskTransfer, agent: AgentCard) -> tuple[bool, str | None]:
    """
    1. 只允许 claim status=proposed 且 TTL/max_hops 仍有效的任务。
    2. 如果 task.target_agent_id 不为空，则只有该 agent_id 可以 claim；其他 Agent 返回 AGENT_NOT_ASSIGNED。
    3. 如果 task.target_agent_id 为空，则 Agent 必须声明匹配 task_type 或任务 metadata 中要求的 capability。
    4. 通过 SQLite CAS 将状态从 proposed 更新为 accepted；认领方成为 accepted 任务的执行方，并写入 `target_agent_id`。
    5. CAS 成功后追加 action=claim_task 的 audit 事件；CAS 失败返回 CAS_CONFLICT，调用方重读任务。
    """
```

claim 是一次明确的状态转换，不订阅任务流，也不承担调度队列语义。任务发现仍可通过普通 `discover`、`GET /tasks` 或 CLI 查询完成；Phase 1.2 不引入长轮询、WebSocket streaming、Redis/Postgres/gRPC/Cloud Bridge。

CAS 成功后，MAC 会把原本未指定执行方的任务更新为 `target_agent_id=<claimer>`。这让后续 `start_task`、quality evidence、completion 和审计查询都能从任务快照本身看到实际执行方，而不只依赖 audit trail 推断。旧版源/目标 agent 别名不再进入协议模型；调用方必须使用 `source_agent_id` / `target_agent_id`。

`claim_next_task(best_effort=True)` 是一个显式降级模式：它仍只扫描 `proposed` 且对当前 agent 可见的任务，但会按该 agent 对任务 required capability 的 observed `success_rate` 排序，再按 priority 和账本顺序打破平局。默认 `best_effort=False` 时仍要求声明 capability 与任务 required capability 精确匹配。

`discover(capability=...)` 也必须使用 observed capability metrics：先筛选声明了该 capability 且在线/负载/项目上下文匹配的 Agent，再按该 capability 下的 observed `success_rate`、样本数和 load 排序。没有历史样本的 Agent 不被剔除，但不会优先于已有更高成功率证据的候选。

### 9.7 Local Agent Adapter Loop（Phase 1.3）

```python
def run_once(agent: AgentCard, capability: str, handler: Callable[[TaskTransfer], TaskRunResult]) -> TaskTransfer | None:
    """
    1. register：写入或刷新 AgentCard。
    2. claim：按 capability 认领一条 proposed task；没有任务时返回 None。
    3. start：将 accepted task 标记为 running。
    4. handler/command：调用 adapter 配置的 handler。命令型 handler 只执行配置传入的 command vector。
    5. quality：写入 command、status、evidence、output、error_code 等质量证据。
    6. complete/fail：handler 通过则走 quality gate 并 complete；失败则 fail。
    7. observe outcome：按 succeeded/failed、duration、error_code 更新 observed capability metrics。
    """
```

`TaskRunResult` 是 handler 和 runner 之间的最小结果合同：

```python
class TaskRunResult(BaseModel):
    status: str                 # passed | failed
    command: str                # adapter 实际配置和执行的命令/动作名称
    evidence: list[str] = []    # test_output, coverage_report, review_notes 等
    output: str = ""            # 捕获输出或 handler 摘要
    error_code: str | None = None
```

命令型 handler 必须使用显式配置：

```python
command_task_handler(
    command=["python", "-m", "pytest", "tests/test_registry_service.py", "-q"],
    cwd=project_root,
    timeout_seconds=60,
    evidence_on_success=["test_output"],
)
```

安全边界：
- MAC 不从 `TaskPayload.summary`、`requirements`、`metadata`、`test_commands` 或任何 payload 字段拼接 shell 命令。
- 命令执行使用 command vector、`shell=False`、超时、可选 cwd/env 边界和输出捕获；它不是沙箱隔离。
- adapter loop 是一次性本地执行器，不是 daemon supervisor、streaming log 管道、云端 worker、Redis/Postgres queue 或 gRPC service。

### 9.8 Local Agent Adapter Templates（Phase 1.4）

Phase 1.4 的模板层是 Phase 1.3 runner 的轻量配置复用层，不改变状态机：

```python
template = pytest_agent_template(
    agent_id="pytest-runner",
    name="Pytest Runner",
    pytest_args=["tests", "-q"],
)

runner = template.create_runner(registry=registry)
runner.run_once()
```

模板职责：
- `LocalAgentTemplate`：保存 `agent_id`、`name`、单一 `capability`、handler、可选 `project_context` 和 metadata。
- `command_agent_template()`：创建显式 command vector 驱动的 adapter template。
- `pytest_agent_template()`：创建 `python -m pytest ...` 驱动的验证 adapter template。
- `runner_from_template()`：把 template 转成 `LocalAgentRunner`，供脚本或 CLI 复用。

边界：
- template 不执行任务，只创建 runner。
- template 不读取 YAML、插件、动态 import 或远端配置。
- template 不轮询、不常驻、不做 supervisor。
- command/pytest template 的命令来源仍然只能是 adapter 配置，不能来自 task payload。

### 9.9 Task Visibility（Phase 1.5）

Phase 1.5 提供只读任务可见性，解决多个 agent 和人类操作者在 claim 前无法快速判断“账本里有什么任务、哪些任务对我可见”的问题：

```python
tasks = registry.list_tasks(
    status="proposed",
    capability="write_test",
    agent_id="tester",
    project_context="project-a",
)
```

过滤语义：
- `status`：按当前任务状态过滤；为空时返回所有状态。
- `capability`：按任务 payload type 或 `payload.extra["required_capability"]` 过滤。
- `agent_id`：返回未指定 `target_agent_id` 的任务，以及 `target_agent_id == agent_id` 的任务；排除已显式指派给其他 agent 的任务。
- `project_context`：按项目上下文过滤。

该查询不产生 audit event，不改变 `updated_at`，不把任务标记为 claimed 或 leased。状态改变仍由 `claim_next_task()`、`accept_handoff()`、`start_task()` 等显式操作完成。

### 9.10 Task Evidence Bundle（Phase 1.6）

Phase 1.6 提供按 `task_id` 的只读证据包：

```python
bundle = registry.get_task_evidence("task-123")
```

聚合内容：
- `task`：当前 TaskTransfer 快照。
- `quality_results`：该 task 已提交的质量证据，按写入顺序返回；每条证据带有 `retry_count`，用于区分不同 attempt。
- `audit_trail`：该 task 的 trace audit trail。
- `execution_agent_id`：`task.target_agent_id`。
- `required_capability`：任务 payload type 或 `payload.extra["required_capability"]`。
- `observed_capability_score`：执行 agent 在该 capability 下的 observed score；没有执行 agent 时为 `None`。

边界：
- 证据包不重新执行测试，不重新评估 quality gate。
- 证据包不写 audit、不改变 `updated_at`、不改变任务状态。
- 证据包是 review/handoff/debug 视图，不是最终验收结论；最终完成仍由 `complete_task()` 和 quality gate 决定。

### 9.11 Quality Gate Preview（Phase 1.7）

Phase 1.7 提供按 `task_id` 的只读质量门预览：

```python
preview = registry.preview_quality_gate("task-123")
```

返回内容：
- `allowed` / `reason`：复用 `evaluate_quality_gate()` 的当前判断。
- `required_commands` / `required_evidence`：TestContract 要求。
- `passed_commands` / `present_evidence`：当前已通过的质量结果中出现的命令和证据。
- `missing_commands` / `missing_evidence`：当前阻塞 completion 的缺口。
- `quality_results_count`：当前 attempt 内已落账质量结果数量。

边界：
- preview 不调用 `complete_task()`，不改变任务状态。
- preview 不写 audit，不改变 `updated_at`。
- preview 是 preflight/debug 信息；真正完成任务时仍由 `complete_task()` 重新执行 quality gate。

### 9.12 Task Readiness / Next Action Preview（Phase 1.8）

Phase 1.8 提供按 `task_id` 的只读下一步建议：

```python
report = registry.preview_task_readiness("task-123")
```

推荐动作规则：
- `proposed` 且未分配执行方：`claim_task`。
- `proposed` 且已有 `target_agent_id`：`accept_handoff`。
- `accepted`：`start_task`。
- `running` 且 quality gate 未满足：`submit_quality_result`，同时返回 `quality_gate_failed:<reason>`。
- `running` 且 quality gate 已满足：`complete_task`。
- `completed`：`none`，阻塞原因为 `task_completed`。
- `failed`：`inspect_failure`，阻塞原因包含 `error_code`。
- `rejected`：`inspect_rejection`。

边界：
- readiness 不改变任务状态，不调用 `_transition()`，不写 audit，不改变 `updated_at`。
- readiness 最多引用 `QualityGatePreview` 的当前结果；它不重新执行测试，也不替代 `complete_task()`。
- readiness 不是调度器、队列、lease、planner 或 worker 编排器；真正取得执行权仍必须调用 `claim_task` 或 `accept_handoff`。

### 9.13 TaskEventBus（Phase 1 release hardening）

Phase 1 提供本机进程内 `TaskEventBus`，用于把 Registry 写操作广播给本进程里的 watcher、UI、debugger 或 adapter 协调层：

- 事件只从 Registry 写路径发布，包括 submit、claim、accept、start、quality result、complete、fail、reject。
- 事件模型包含 `event_id`、`type`、`task_id`、`trace_id`、`actor`、`from_status`、`to_status`、`payload`、`created_at`。
- 订阅方式支持同步 callback，也支持 asyncio queue broadcast；订阅方可按 event type 过滤并主动 close。
- EventBus 不持久化事件，不替代 audit log；audit log 仍是可追溯证据源。
- Phase 1 EventBus 只保证本进程内通知；跨进程、跨机器和可恢复订阅放到 Phase 2 Redis Pub/Sub 或同级传输层。

### 9.14 Failure Recovery（Phase 1.9）

Phase 1 提供最小失败恢复闭环，不引入后台调度器：

- `record_checkpoint(task_id, agent_id, checkpoint)`：把结构化 checkpoint 追加到 `task.metadata["checkpoints"]`，写 `checkpoint_task` audit，并发布 `task_checkpointed`。
- `retry_task(task_id, agent_id, fallback_agent_id=None)`：只允许对 `failed` 任务执行；状态回到 `proposed`，`retry_count += 1`，清空 `error_code`，并把 `target_agent_id` 指向显式 fallback、任务已有 fallback，或清空后重新开放认领。
- `cancel_task(task_id, agent_id, reason)`：把未完成任务标记为 `cancelled`，设置 `error_code=TASK_CANCELLED`，写 `cancel_task` audit，并发布 `task_cancelled`。
- `submit_quality_result()` 会把当前 `task.retry_count` 写入质量证据；`preview_quality_gate()`、`preview_task_readiness()` 和 `complete_task()` 只评估当前 attempt 的质量证据，避免 retry 后复用旧 attempt 的通过结果。
- recovery 操作是显式 API，不会由 `fail_task()` 自动触发；是否重试、转派或取消由人类/上层 planner 决定。

---

## 10. CLI Bridge

第一阶段 CLI 是 Agent 和人类操作者接入 MAC 的最小桥：

```bash
mac registry start --db mac.db
mac agent register --agent-id claude-code --capability write_code --capability code_review
mac task submit --type write_test --context context.json --risk high
mac task status --task-id task-123
mac claim --agent-id pytest-runner --capability write_test
mac task accept --task-id task-123 --agent-id pytest-runner
mac task complete --task-id task-123 --agent-id pytest-runner --evidence results.json
mac ledger audit --trace-id trace-abc
```

CLI bridge 不直接执行复杂 Agent 逻辑，只负责把命令转换成 MAC service 调用并写入 SQLite Task Ledger。

Phase 1.1 CLI 可以增加只读查询和规划辅助命令：

```bash
mac task plan-contract --type write_code --context context.json --risk medium
mac agent metrics --agent-id claude-code
```

这些命令仍然只调用本地 service 和 SQLite 账本，不连接云端协调层。

Phase 1.2 CLI 增加主动认领命令：

```bash
mac claim --agent-id pytest-runner --capability write_test
```

该命令只发起一次 claim service 调用：按 capability 扫描 proposed task，校验 `target_agent_id`，使用 CAS 从 `proposed` 更新到 `accepted`，写入 `claim_task` audit，并将认领方写回 `target_agent_id`。可选 `--best-effort` 会启用 observed success_rate 排序。

Phase 1.3 CLI 可增加一次性本地 adapter 命令：

```bash
mac-agent run-once --db mac.db --agent-id pytest-runner --name "Pytest Runner" --capability validate_tests --command python -m pytest tests -q
```

`--command` 后的剩余参数作为 adapter 配置的 command vector。CLI 负责注册 Agent、claim 一条任务并调用 `LocalAgentRunner.run_once()`；最终输出完成/失败后的 task JSON，或者在没有可认领任务时输出 `null`。该命令不读取 task payload 中的命令字段执行，也不常驻后台。

Phase 1.4 中 `run-once` 内部复用 `command_agent_template()` 创建 runner；这只是实现复用，不改变 CLI 参数、输出 JSON 或一次性执行语义。更复杂的模板注册、插件发现、配置文件加载和后台 worker 仍不属于 Phase 1。

Phase 1.5 CLI 增加只读任务查询命令：

```bash
mac-agent tasks --db mac.db --status proposed --capability write_test --agent-id pytest-runner
```

该命令输出 task JSON 数组，不写入 audit，也不改变任务状态。它用于调试和 claim 前可见性，不是轮询队列或任务租约。

Phase 1.6 CLI 增加只读证据包查询命令：

```bash
mac-agent task-evidence --db mac.db --task-id task-123
```

该命令输出 `TaskEvidenceBundle` JSON；任务不存在时输出 `null`。它不写入 audit，不改变任务状态，也不替代 quality gate。

Phase 1.7 CLI 增加只读质量门预览命令：

```bash
mac-agent quality-preview --db mac.db --task-id task-123
```

该命令输出 `QualityGatePreview` JSON；任务不存在时输出 `null`。它不写入 audit，不改变任务状态，也不替代 `complete_task()`。

Phase 1.8 CLI 增加只读任务就绪度 / 下一步建议命令：

```bash
mac-agent task-readiness --db mac.db --task-id task-123
```

该命令输出 `TaskReadinessReport` JSON；任务不存在时输出 `null`。它不写入 audit，不改变任务状态，不 claim、不 accept、不 start、不 complete、不 fail，也不替代 `claim_task` 的执行权获取或 `complete_task()` 的最终 quality gate。

---

## 11. HTTP Adapter（Phase 1.1）

minimal FastAPI HTTP adapter 是 Registry service 的薄封装：
- 请求/响应模型复用协议模型，避免 HTTP 层定义第二套领域对象
- 所有写操作先进入 service，再由 service 写入 SQLite Task Ledger 和 audit log
- adapter 不保存内存会话状态，不做跨机器 relay，不直接运行测试命令
- 错误响应使用标准错误码，并保留 `trace_id` 方便审计

该 adapter 的目标是让本地多 Agent、CLI 或调试脚本通过 HTTP 访问同一套 MAC 闭环；它不是 Cloud Bridge，也不要求 Redis、Postgres、gRPC 或反向代理部署。

---

## 12. 目录结构（目标形态）

```
multi-agent-coordinator/
├── pyproject.toml
├── README.md
├── src/mac/
│   ├── __init__.py
│   ├── cli.py
│   ├── agent.py
│   ├── registry.py
│   ├── protocol/
│   │   ├── models.py
│   │   ├── constants.py
│   │   └── errors.py
│   ├── ledger/
│   │   ├── base.py
│   │   └── sqlite.py
│   ├── events.py
│   ├── transport/
│   │   └── http_ws.py
│   ├── runner/
│   │   ├── local.py
│   │   └── templates.py
│   └── quality/
│       ├── gate.py
│       └── planner.py
├── tests/
│   ├── test_protocol_models.py
│   ├── test_sqlite_ledger.py
│   ├── test_registry_service.py
│   ├── test_quality_gate.py
│   ├── test_cli_bridge.py
│   └── test_e2e_handoff.py
├── docs/
│   ├── SPEC.md
│   └── superpowers/plans/
└── examples/
    └── local_handoff.py
```

第一阶段目录中不出现 `proto/`、`grpc_transport.py`、`redis_pubsub.py`、Postgres 后端、Cloud bridge 模块或空壳 in-process transport wrapper。进程内调用方直接使用 `Registry`；Phase 1.1 的 HTTP adapter 落在 `http_ws.py`，并必须复用本地 service、SQLite 账本和 ContextBundle 交接模型。

---

## 13. 开发计划（第一阶段）

目标：MVP 可用，跑通“注册 Agent -> 提交任务 -> 发现候选 -> handoff -> 执行更新 -> 写入验证结果 -> quality gate -> 完成并查询审计”的闭环。

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | 协议模型：AgentCard、ContextBundle、TestContract、TaskTransfer、AuditEntry | `src/mac/protocol/models.py` |
| 2 | SQLite Task Ledger：表结构、CRUD、CAS、审计查询 | `src/mac/ledger/sqlite.py` |
| 3 | Registry service：注册、发现、任务状态机、handoff | `src/mac/registry.py` |
| 4 | Quality Gate：按 TestContract 校验验证结果 | `src/mac/quality/gate.py` |
| 5 | Direct in-process API：测试和嵌入式运行直接复用 `Registry` | `src/mac/registry.py` |
| 6 | Local TaskEventBus：本机写事件 broadcast，支持同步回调和 asyncio queue | `src/mac/events.py` |
| 7 | HTTP/WebSocket transport：本机多 Agent 通信 | `src/mac/transport/http_ws.py` |
| 8 | CLI bridge：命令行接入核心操作 | `src/mac/cli.py` |
| 9 | E2E：两 Agent 本地交接和账本审计 | `tests/test_e2e_handoff.py` |

### 13.1 Phase 1.1 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | Observed capability metrics：从任务状态、质量结果和审计事件更新观察指标 | `capability_metrics` ledger 表与查询接口 |
| 2 | Automatic TestContract planner：按任务类型、风险和 ContextBundle 生成默认验证合同 | `src/mac/quality/planner.py` |
| 3 | Minimal FastAPI HTTP adapter：暴露本地 Registry service 核心端点 | `src/mac/transport/http_ws.py` |
| 4 | CLI 辅助：合同规划和能力指标查询 | `src/mac/cli.py` |
| 5 | E2E：HTTP 提交任务、自动规划合同、写入质量结果、更新 metrics | `tests/test_e2e_handoff.py` |

### 13.2 Phase 1.2 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | Capability-based task claiming：Agent 按声明能力主动认领 proposed task | Registry service claim 操作 |
| 2 | 账本 CAS：claim 使用 `expected_status="proposed"` 更新到 `accepted`，尊重已有 `target_agent_id`，并将认领方固化为 `target_agent_id` | SQLite ledger 状态更新与审计 |
| 3 | 审计：成功 claim 写入 `claim_task` audit 事件 | `audit_log` 轨迹 |
| 4 | CLI/HTTP：暴露一次性 claim 命令和端点 | `mac claim`、`POST /agents/{agent_id}/claim` |

### 13.3 Phase 1.3 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | LocalAgentRunner：封装 register、claim、start、handler、quality、complete/fail、observe outcome 的单次闭环 | `src/mac/runner/local.py` |
| 2 | Handler result：结构化返回 command、status、evidence、output、error_code | `TaskRunResult` |
| 3 | Controlled command handler：执行 adapter 配置的 command vector，记录输出，映射 `COMMAND_FAILED` 与 `COMMAND_TIMEOUT` | `command_task_handler` |
| 4 | CLI run-once：注册 agent、认领一条任务、执行配置命令并输出最终 task | `mac-agent run-once` |
| 5 | 示例：展示本地 adapter loop 和审计轨迹 | `examples/local_runner.py` |

### 13.4 Phase 1.4 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | LocalAgentTemplate：封装 agent 身份、capability、handler、project_context 和 metadata | `src/mac/runner/templates.py` |
| 2 | Command template：复用 `command_task_handler`，生成显式 command vector 驱动的 runner | `command_agent_template()` |
| 3 | Pytest template：提供测试验证 adapter 的标准入口，执行 `python -m pytest ...` | `pytest_agent_template()` |
| 4 | CLI 复用：`run-once` 内部走 command template，保持原参数和输出不变 | `src/mac/cli.py` |
| 5 | 回归测试：覆盖模板创建、runner 闭环、project_context override、payload 命令不执行和 pytest 模板 | `tests/test_runner_templates.py` |

### 13.5 Phase 1.5 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | Registry 只读任务查询：按 status、capability、agent assignment 和 project_context 过滤任务 | `Registry.list_tasks()` |
| 2 | Registry 单任务读取：返回 task 或 None，供 HTTP/CLI 复用 | `Registry.get_task()` |
| 3 | CLI 查询：输出过滤后的 task JSON 数组 | `mac-agent tasks` |
| 4 | HTTP 查询：本地服务暴露任务列表和单任务读取 | `GET /tasks`、`GET /tasks/{task_id}` |
| 5 | 回归测试：覆盖只读过滤、agent assignment、无 audit 副作用、CLI 和 HTTP 行为 | `tests/test_task_listing.py`、`tests/test_cli_tasks.py`、`tests/test_http_tasks.py` |

### 13.6 Phase 1.6 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | TaskEvidenceBundle：定义只读证据包模型，聚合 task、quality、audit 和 observed score | `TaskEvidenceBundle` |
| 2 | Registry 证据包查询：按 task_id 聚合任务快照、质量证据、审计轨迹和执行 agent 能力分数 | `Registry.get_task_evidence()` |
| 3 | CLI 查询：输出证据包 JSON，任务不存在时输出 `null` | `mac-agent task-evidence` |
| 4 | HTTP 查询：本地服务暴露证据包读取，任务不存在返回 404 | `GET /tasks/{task_id}/evidence` |
| 5 | 回归测试：覆盖聚合内容、缺失任务、只读语义、CLI 和 HTTP 行为 | `tests/test_task_evidence.py`、`tests/test_cli_task_evidence.py`、`tests/test_http_task_evidence.py` |

### 13.7 Phase 1.7 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | QualityGatePreview：定义只读质量门预检模型，包含允许状态、原因、已满足和缺失项 | `QualityGatePreview` |
| 2 | Registry 预览：复用 `evaluate_quality_gate()`，聚合 required/present/missing commands/evidence | `Registry.preview_quality_gate()` |
| 3 | CLI 查询：输出质量门预览 JSON，任务不存在时输出 `null` | `mac-agent quality-preview` |
| 4 | HTTP 查询：本地服务暴露质量门预览，任务不存在返回 404 | `GET /tasks/{task_id}/quality-preview` |
| 5 | 回归测试：覆盖缺失证据、已满足合同、无合同、缺失任务、只读语义、CLI 和 HTTP 行为 | `tests/test_quality_preview.py`、`tests/test_cli_quality_preview.py`、`tests/test_http_quality_preview.py` |

### 13.8 Phase 1.8 增量

| 步骤 | 内容 | 产出 |
|------|------|------|
| 1 | TaskReadinessReport：定义只读下一步建议模型，包含当前状态、执行 agent、required capability、推荐动作和阻塞原因 | `TaskReadinessReport` |
| 2 | Registry 预览：按 task 状态和 quality preview 计算 `next_action`，并保持只读语义 | `Registry.preview_task_readiness()` |
| 3 | CLI 查询：输出 readiness JSON，任务不存在时输出 `null` | `mac-agent task-readiness` |
| 4 | HTTP 查询：本地服务暴露 readiness 预览，任务不存在返回 404 | `GET /tasks/{task_id}/readiness` |
| 5 | 回归测试：覆盖 proposed/accepted/running/completed/failed、canonical target agent、required capability override、缺失任务、只读语义、CLI 和 HTTP 行为 | `tests/test_task_readiness.py`、`tests/test_cli_task_readiness.py`、`tests/test_http_task_readiness.py` |

---

## 14. Phase 2 路线

Phase 2 在第一阶段账本、协议和 quality gate 稳定后再展开：
- gRPC + Protobuf：用于高吞吐和跨语言 SDK
- Redis Pub/Sub：用于多进程事件分发和临时消息通道
- PostgreSQL：用于生产级多实例部署和长周期审计
- Cloud Bridge：用于跨机器、CI/CD 与本地 Agent 的桥接
- Hybrid Bridge：用于本地优先、云端兜底的双通道协调
- 能力认证：从 Phase 1.1 observed metrics 升级到 verified capability

---

## 15. 标准错误码

| 错误码 | 含义 | 接收方动作 |
|--------|------|------------|
| `TASK_TYPE_UNSUPPORTED` | 不支持该任务类型 | reject，不重试 |
| `CONTEXT_URI_INVALID` | 上下文引用不可读 | 可重试，失败后 fallback |
| `CAPABILITY_INSUFFICIENT` | 能力不足 | 转派其他 Agent |
| `AGENT_NOT_ASSIGNED` | 任务已有显式 `target_agent_id` 且认领方不是目标 Agent | 不允许 claim，重读任务或转由目标 Agent 处理 |
| `PAYLOAD_TOO_LARGE` | payload 超过限制 | 改用 ContextBundle 引用 |
| `SCHEMA_VERSION_MISMATCH` | 协议版本不兼容 | 降级或拒绝 |
| `MAX_HOPS_EXCEEDED` | 超过最大流转次数 | failed，不重试 |
| `TTL_EXPIRED` | 任务过期 | failed，不重试 |
| `CAS_CONFLICT` | 状态竞争 | 重读后重试 |
| `AGENT_OFFLINE` | 目标 Agent 离线 | fallback |
| `QUALITY_GATE_FAILED` | 验证合同未通过 | failed 或人工 override |
| `TRANSFER_REJECTED` | 接收方显式拒绝 | 尝试其他候选者 |
| `TEST_CONTRACT_PLANNING_FAILED` | 无法生成默认验证合同 | 提供显式 TestContract 后重试 |
| `CAPABILITY_METRICS_UNAVAILABLE` | 能力观察指标不可用或尚无数据 | 回退到 AgentCard 声明和负载排序 |
| `HANDLER_ERROR` | adapter handler 抛出异常 | failed，记录输出/异常摘要 |
| `COMMAND_FAILED` | adapter 配置的命令非零退出 | failed，记录 stdout/stderr |
| `COMMAND_TIMEOUT` | adapter 配置的命令超过 timeout | failed，记录已捕获输出 |

---

## 16. 已知限制

- 第一阶段只保证本地和单项目协作，不承诺跨公网、跨租户、跨数据中心能力
- ContextBundle 的质量决定 handoff 质量；MAC 能强制结构，但不能替调用方理解业务
- Quality Gate 只判断验证证据是否满足合同，不判断测试本身是否写得聪明
- Phase 1.1 的 TestContract planner 只给出默认合同，不保证覆盖所有项目特定风险
- Observed capability metrics 是观察值，不是认证、授权或 SLA
- FastAPI adapter 是本地 HTTP 接入层，不是 Cloud Bridge、gRPC gateway 或生产级多实例控制面
- Phase 1.2 的 task claiming 是一次性状态转换，不是长轮询、WebSocket streaming、Redis 队列、Postgres 锁、gRPC stream 或 Cloud Bridge
- Phase 1.3 的 LocalAgentRunner 是一次性本地 adapter loop，不是 daemon、streaming、Cloud Bridge、Redis/Postgres queue 或 gRPC worker
- Phase 1.3 的 command handler 只能执行 adapter 显式配置的命令；task payload 不能作为可执行命令来源
- Phase 1.4 的 LocalAgentTemplate 只复用本地 adapter 配置，不是 worker 编排器、插件系统、配置中心或长期运行进程
- Phase 1.5 的 task visibility 是只读查询，不是轮询、租约、优先级队列、调度器或 claim 的替代品
- Phase 1.6 的 task evidence bundle 是只读聚合视图，不重新执行测试、不重新评估 quality gate、不作为最终验收结论
- Phase 1.7 的 quality preview 是只读预检，不 complete、不 fail、不替代 `complete_task()` 的最终 quality gate
- Phase 1.8 的 task readiness 是只读下一步建议，不 claim、不 accept、不 start、不 complete、不 fail，也不是调度器、队列、lease 或 worker 编排器
- SQLite 适合 MVP 和单机工作流；多实例强一致性放到 Phase 2 的 PostgreSQL 路线

---

*设计文档，v1.14，按第一阶段 release hardening + 综合改进决策更新。*
