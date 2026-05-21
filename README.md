# Multi-Agent Coordinator (MAC)

**Version:** 0.1.4 | **License:** MIT

---

## What Is MAC?

MAC is a lightweight coordination layer for AI coding agents — a task ledger, context broker, quality gate, and handoff protocol for multi-agent Python development.

It helps multiple AI agents work together: agents register with their capabilities, submit tasks, claim work, execute with verified quality gates, and hand off context to the next agent.

**MAC is NOT:**
- An MCP replacement (MCP handles resources/tools)
- A LangGraph/CrewAI replacement (MAC is a handoff protocol, not an execution engine)
- A task queue (claim is a one-shot state transition, not a lease)
- A test framework (TestContract specifies what evidence is required; tests run elsewhere)
- A streaming/log service (adapter loops are one-shot)

---

## Install

```bash
pip install mac-agent                     # CLI + local ledger
pip install "mac-agent[http]"             # + FastAPI HTTP adapter
pip install -e ".[dev]"                    # development with tests
```

---

## Quick Start

```bash
mac-agent register --agent-id claude --name Claude --capability write_code
mac-agent submit --task-id t1 --source-agent-id alice --type write_code --summary "Add auth handler"
mac-agent tasks --status proposed
mac-agent claim --agent-id claude --capability write_code
mac-agent start --task-id t1 --agent-id claude
mac-agent complete --task-id t1 --agent-id claude
```

---

## Python API

```python
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage
from mac.protocol.messages import AgentCard, AgentCapability, TaskTransfer, TaskPayload

registry = Registry(SQLiteStorage("mac.db"))

# Register agent
agent = AgentCard(agent_id="worker-1", name="Worker", capabilities=[AgentCapability(name="write_code")])
registry.register(agent)

# Submit task
task = TaskTransfer(
    task_id="t1", source_agent_id="alice",
    payload=TaskPayload(type="write_code", summary="Write auth handler")
)
registry.submit_task(task)

# Claim and complete
task = registry.claim_next_task(agent_id="worker-1", capability="write_code")
registry.start_task(task.task_id, "worker-1")
# ... run work, submit quality results ...
registry.complete_task(task.task_id, "worker-1")
```

---

## HTTP Adapter

```python
from mac.transport.http_ws import create_app
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage

app = create_app(Registry(SQLiteStorage("mac.db")))
# Run with: uvicorn app:app --port 8000
```

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `GET/POST` | `/agents` | List or register agents |
| `GET` | `/agents/{id}` | Get agent by ID |
| `POST` | `/agents/heartbeat` | Agent heartbeat |
| `POST` | `/agents/{id}/claim` | Claim a task |
| `GET/POST` | `/tasks` | List or submit tasks |
| `GET` | `/tasks/{id}` | Get task by ID |
| `GET` | `/tasks/{id}/evidence` | Task evidence bundle (read-only) |
| `GET` | `/tasks/{id}/quality-preview` | Quality gate preview (read-only) |
| `GET` | `/tasks/{id}/readiness` | Task readiness report (read-only) |
| `POST` | `/tasks/{id}/accept` | Accept handoff |
| `POST` | `/tasks/{id}/start` | Start task |
| `POST` | `/tasks/{id}/complete` | Complete task |
| `POST` | `/tasks/{id}/fail` | Fail task |
| `POST` | `/tasks/{id}/checkpoint` | Record checkpoint |
| `POST` | `/tasks/{id}/retry` | Retry failed task |
| `POST` | `/tasks/{id}/cancel` | Cancel task |
| `GET` | `/ledger/{trace_id}` | Audit trail |

---

## Examples

```bash
python examples/local_handoff.py   # two-agent handoff via Registry
python examples/local_runner.py    # LocalAgentRunner adapter loop
```

---

## Architecture

```
src/mac/
├── protocol/          # Domain models: messages.py, errors.py
├── storage/           # SQLite ledger: sqlite.py
├── registry.py        # Task lifecycle, agent discovery, quality gate
├── quality/           # Quality gate evaluation: gate.py
├── runner/            # LocalAgentRunner and templates
│   ├── local.py       # LocalAgentRunner.run_once(), TaskRunResult
│   └── templates.py   # LocalAgentTemplate, command_agent_template
├── testing/           # TestContract, planner
│   ├── contracts.py   # Risk-based TestContract
│   └── planner.py     # Automatic TestContract generation
├── transport/
│   └── http_ws.py    # FastAPI adapter
├── cli.py             # 20 subcommands
└── events.py          # TaskEventBus
```

---

## State Machine

```
proposed → accepted → running → completed
    ↓          ↓           ↓
  rejected   rejected    failed
```

Also: `cancelled`, `superseded`.

---

## Risk-Based TestContract

| Risk | Required Command | Required Evidence |
|------|-----------------|-------------------|
| low | pytest related tests or smoke test | test_output |
| medium | python -m pytest tests | test_output, changed_files |
| high | python -m pytest --cov | test_output, coverage_report, review_notes |

---

## Testing

```bash
python -m pytest -q          # 118 tests
python examples/local_handoff.py
python examples/local_runner.py
```

================================================================================

# Multi-Agent Coordinator (MAC)

**版本：** 0.1.4 | **协议：** MIT

---

## 什么是 MAC？

MAC 是 AI 编程 Agent 的轻量协调层——任务账本、上下文交接、质量门验证、Agent 间交接协议，支持多 Agent Python 开发工作流。

多 Agent 协作：Agent 注册能力、提交任务、认领工作、执行并通过质量验证、上下文交接给下一个 Agent。

**MAC 不是：**
- MCP 的替代品（MCP 负责资源/工具）
- LangGraph/CrewAI 的替代品（MAC 是交接协议，不是执行引擎）
- 任务队列（claim 是单次状态转换，不是租约）
- 测试框架（TestContract 指定需要什么证据；测试在别处运行）
- 流式日志服务（适配器循环是一次性的）

---

## 安装

```bash
pip install mac-agent                     # CLI + 本地账本
pip install "mac-agent[http]"            # + FastAPI HTTP 适配器
pip install -e ".[dev]"                  # 开发依赖（含测试）
```

---

## 快速上手

```bash
mac-agent register --agent-id claude --name Claude --capability write_code
mac-agent submit --task-id t1 --source-agent-id alice --type write_code --summary "添加 auth handler"
mac-agent tasks --status proposed
mac-agent claim --agent-id claude --capability write_code
mac-agent start --task-id t1 --agent-id claude
mac-agent complete --task-id t1 --agent-id claude
```

---

## Python API

```python
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage
from mac.protocol.messages import AgentCard, AgentCapability, TaskTransfer, TaskPayload

registry = Registry(SQLiteStorage("mac.db"))

# 注册 Agent
agent = AgentCard(agent_id="worker-1", name="Worker", capabilities=[AgentCapability(name="write_code")])
registry.register(agent)

# 提交任务
task = TaskTransfer(
    task_id="t1", source_agent_id="alice",
    payload=TaskPayload(type="write_code", summary="编写 auth handler")
)
registry.submit_task(task)

# 认领并完成
task = registry.claim_next_task(agent_id="worker-1", capability="write_code")
registry.start_task(task.task_id, "worker-1")
# ... 执行工作，提交质量证据 ...
registry.complete_task(task.task_id, "worker-1")
```

---

## HTTP 适配器

```python
from mac.transport.http_ws import create_app
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage

app = create_app(Registry(SQLiteStorage("mac.db")))
# 运行方式: uvicorn app:app --port 8000
```

**端点：**

| 方法 | 路径 | 描述 |
|--------|------|------|
| `GET` | `/` | 健康检查 |
| `GET/POST` | `/agents` | 列出或注册 Agent |
| `GET` | `/agents/{id}` | 按 ID 获取 Agent |
| `POST` | `/agents/heartbeat` | Agent 心跳 |
| `POST` | `/agents/{id}/claim` | 认领任务 |
| `GET/POST` | `/tasks` | 列出或提交任务 |
| `GET` | `/tasks/{id}` | 按 ID 获取任务 |
| `GET` | `/tasks/{id}/evidence` | 任务证据包（只读） |
| `GET` | `/tasks/{id}/quality-preview` | 质量门预览（只读） |
| `GET` | `/tasks/{id}/readiness` | 任务就绪度报告（只读） |
| `POST` | `/tasks/{id}/accept` | 接受交接 |
| `POST` | `/tasks/{id}/start` | 开始任务 |
| `POST` | `/tasks/{id}/complete` | 完成任务 |
| `POST` | `/tasks/{id}/fail` | 标记失败 |
| `POST` | `/tasks/{id}/checkpoint` | 记录检查点 |
| `POST` | `/tasks/{id}/retry` | 重试失败任务 |
| `POST` | `/tasks/{id}/cancel` | 取消任务 |
| `GET` | `/ledger/{trace_id}` | 审计轨迹 |

---

## 示例

```bash
python examples/local_handoff.py   # 双 Agent 通过 Registry 交接
python examples/local_runner.py   # LocalAgentRunner 适配器循环
```

---

## 架构

```
src/mac/
├── protocol/          # 领域模型：messages.py, errors.py
├── storage/            # SQLite 账本：sqlite.py
├── registry.py         # 任务生命周期、Agent 发现、质量门
├── quality/            # 质量门评估：gate.py
├── runner/             # LocalAgentRunner 和模板
│   ├── local.py        # LocalAgentRunner.run_once(), TaskRunResult
│   └── templates.py    # LocalAgentTemplate, command_agent_template
├── testing/            # TestContract, planner
│   ├── contracts.py     # 风险分级 TestContract
│   └── planner.py       # 自动 TestContract 生成
├── transport/
│   └── http_ws.py      # FastAPI 适配器
├── cli.py              # 20 个子命令
└── events.py           # TaskEventBus
```

---

## 状态机

```
proposed → accepted → running → completed
    ↓          ↓           ↓
  rejected   rejected    failed
```

还支持：`cancelled`、`superseded`。

---

## 风险分级 TestContract

| 风险 | 必需命令 | 必需证据 |
|------|---------|---------|
| low | pytest related tests or smoke test | test_output |
| medium | python -m pytest tests | test_output, changed_files |
| high | python -m pytest --cov | test_output, coverage_report, review_notes |

---

## 测试

```bash
python -m pytest -q          # 118 个测试
python examples/local_handoff.py
python examples/local_runner.py
```