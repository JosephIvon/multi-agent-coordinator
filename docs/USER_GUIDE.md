# MAC 使用手册

> Multi-Agent Coordinator v0.6.0 — 让多个 AI 编码工具在同一项目中协作的本地账本

---

## 1. MAC 是什么

MAC 是一个**本地协作账本**，不是执行引擎。它解决的核心问题：

> 你同时用 Claude Code、Qoder、Cursor 等多个 AI 工具改同一个项目，谁来协调？

MAC 给你：
- **任务分配**：谁做什么，谁依赖谁
- **上下文传递**：A 做完了，B 自动看到 A 改了哪些文件、有什么风险
- **质量把关**：任务完成前必须通过质量门
- **审核流程**：重要改动需要人工审核才能合入
- **状态恢复**：Agent 崩溃了，任务不会永远卡住

---

## 2. 安装

```bash
# 基础安装
pip install mac-agent

# 带 HTTP 服务
pip install "mac-agent[http]"

# 带 MCP Server（给 AI 工具用）
pip install "mac-agent[mcp]"

# 开发模式
pip install -e ".[dev]"
```

---

## 3. 三种使用方式

### 方式 A：CLI 命令行（人类用）

```bash
mac-agent <子命令> --db mac.db
```

### 方式 B：Python API（脚本/集成用）

```python
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger

registry = Registry(SQLiteTaskLedger("mac.db"))
```

### 方式 C：MCP Server（AI 工具用）

```bash
# 启动 MCP Server
mac-mcp-server

# 在 Claude Code 中配置
claude mcp add mac -- mac-mcp-server
```

配置后，AI 工具可以直接调用 `mac_submit_task`、`mac_claim_task` 等 14 个工具。

---

## 4. 实战场景

### 场景 1：两个人类开发者协作

Alice 写代码，Bob 写测试。Alice 先完成，Bob 才能开始。

```bash
# Alice 注册自己
mac-agent register --agent-id alice --name Alice --capability write_code

# Bob 注册自己
mac-agent register --agent-id bob --name Bob --capability write_test

# 创建协作计划
mac-agent plan create --plan-id ship-login --goal "Ship login feature" --created-by planner
mac-agent plan activate --plan-id ship-login

# Alice 提交写代码的任务
mac-agent submit --task-id write-auth --source-agent-id planner \
  --type write_code --summary "Implement auth handler" --plan-id ship-login

# Bob 提交写测试的任务（依赖 Alice 先完成）
mac-agent submit --task-id test-auth --source-agent-id planner \
  --type write_test --summary "Test auth handler" --plan-id ship-login \
  --depends-on write-auth --target-module src/auth.py --coverage-goal 80

# 查看谁可以认领什么
mac-agent ready-tasks --capability write_code
# → write-auth（Bob 的 test-auth 被依赖阻塞）

# Alice 认领并开始工作
mac-agent claim --agent-id alice --capability write_code
mac-agent start --task-id write-auth --agent-id alice

# Alice 完成了，保存交接记录
mac-agent complete --task-id write-auth --agent-id alice
mac-agent handoff --task-id write-auth --agent-id alice \
  --changed-file src/auth.py --verification "pytest -q:pass:all pass" \
  --risk "manual browser test needed"

# 现在 Bob 的任务解锁了
mac-agent ready-tasks --capability write_test
# → test-auth（write-auth 已完成，依赖解锁）

# Bob 认领并开始
mac-agent claim --agent-id bob --capability write_test
mac-agent start --task-id test-auth --agent-id bob

# Bob 查看上游交接信息（Alice 改了什么、风险是什么）
mac-agent worker-packet --task-id test-auth --agent-id bob

# Bob 完成测试
mac-agent complete --task-id test-auth --agent-id bob
```

### 场景 2：AI Agent 一键接活

Agent 进入项目，一步完成"找任务→认领→看上下文"：

```bash
mac-agent next --agent-id claude --capability write_code
```

输出：
```
---MAC-TASK: {"task_id": "write-auth", "status": "running"}---
# Worker Task: write-auth
...
```

`---MAC-TASK:---` 行是机器可解析的 JSON，后面是给人看的 Markdown 工作指引。

### 场景 3：审核流程（require_review）

重要改动需要人工审核才能标记完成：

```bash
# 设置环境变量启用审核
export MAC_REQUIRE_REVIEW=1

# 或者在 Python 中
from mac.protocol.messages import CoordinationPolicy
policy = CoordinationPolicy(require_review=True)
registry = Registry(SQLiteTaskLedger("mac.db"), policy=policy)
```

启用后，`complete_task()` 被禁止，必须走审核流程：

```bash
# Agent 完成工作后，提交审核
mac-agent review-lifecycle mark-ready --task-id write-auth --agent-id alice

# 审核人查看审核包（含代码变更、质量证据、风险提示）
mac-agent review-packet --task-id write-auth

# 审核通过
mac-agent review-lifecycle accept --task-id write-auth --reviewer-id reviewer

# 审核不通过（自动记录冲突）
mac-agent review-lifecycle reject --task-id write-auth --reviewer-id reviewer \
  --reason "Missing error handling for timeout"
```

### 场景 4：审核人能力验证

只允许有 `review_code` 能力的 Agent 审核：

```bash
export MAC_REVIEWER_CAPABILITY=review_code
```

```python
policy = CoordinationPolicy(require_review=True, reviewer_capability="review_code")
registry = Registry(SQLiteTaskLedger("mac.db"), policy=policy)

# 没有review_code能力的Agent调用accept_review会被拒绝
# 只有注册了review_code能力的Agent才能审核
```

### 场景 5：Agent 崩溃恢复

Agent 崩了，任务卡在 `running` 状态。用 TTL 自动恢复：

```bash
# 查看卡住的任务
mac-agent tasks --status running

# 过期超过 TTL 的任务自动标记为 failed
mac-agent expire-stale

# 或者自动重试（如果重试次数没用完）
mac-agent expire-stale --auto-retry

# 手动重试某个失败的任务
mac-agent retry --task-id write-auth --agent-id alice
```

同样，Agent 心跳超时会自动下线：

```bash
# 超过 300 秒没心跳的 Agent 自动设为 offline
mac-agent expire-stale-agents --timeout 300
```

### 场景 6：项目状态总览

一条命令看全貌：

```bash
mac-agent dashboard
```

输出示例：
```
MAC Dashboard
==================================================

Plans (1 active):
  ship-login  1 completed, 1 running, 1 proposed

Tasks:
  1 ready to claim
  1 in-flight (running)

Agents:
  2 online (alice, bob)

Conflicts (1 unresolved):
  reject_review: Missing error handling for timeout

Metrics:
  cycle_time   0.23s  |  handoff_rate  100%  |  quality_rate  100%
  retry_rate   0%     |  conflict_rate  33%   |  active_agents  2
```

---

## 5. 完整 CLI 命令参考

### Agent 管理

| 命令 | 作用 |
|------|------|
| `mac-agent register` | 注册一个 Agent（能力、路径边界） |
| `mac-agent discover` | 按能力搜索已注册的 Agent |
| `mac-agent expire-stale-agents` | 下线心跳超时的 Agent |

### 任务生命周期

| 命令 | 作用 |
|------|------|
| `mac-agent submit` | 提交任务（含依赖关系、优先级、TTL） |
| `mac-agent ready-tasks` | 列出可认领的任务（依赖已满足） |
| `mac-agent claim` | 认领下一个可用的任务 |
| `mac-agent start` | 标记任务开始执行 |
| `mac-agent complete` | 完成任务（需通过质量门） |
| `mac-agent fail` | 标记任务失败 |
| `mac-agent retry` | 重试失败的任务 |
| `mac-agent cancel` | 取消任务 |
| `mac-agent next` | 一键：认领 + 开始 + 输出工作指引 |
| `mac-agent status` | 查看单个任务状态 |
| `mac-agent tasks` | 列出所有任务 |
| `mac-agent expire-stale` | 过期卡住的任务（支持 --auto-retry） |

### 审核流程

| 命令 | 作用 |
|------|------|
| `mac-agent review-lifecycle mark-ready` | 提交审核（running → review_ready） |
| `mac-agent review-lifecycle accept` | 审核通过（review_ready → completed） |
| `mac-agent review-lifecycle reject` | 审核驳回（review_ready → rejected，自动记录冲突） |

### 协作上下文

| 命令 | 作用 |
|------|------|
| `mac-agent handoff` | 保存/查看结构化交接记录 |
| `mac-agent worker-packet` | 生成工作指引 Markdown（给执行 Agent 看） |
| `mac-agent review-packet` | 生成审核指引 Markdown（给审核人看） |

### 质量门

| 命令 | 作用 |
|------|------|
| `mac-agent quality` | 提交质量证据（如 pytest 结果） |
| `mac-agent quality-preview` | 预览质量门是否满足 |
| `mac-agent task-readiness` | 预览任务下一步推荐操作 |
| `mac-agent task-evidence` | 查看任务全部证据 |

### 计划管理

| 命令 | 作用 |
|------|------|
| `mac-agent plan create` | 创建协作计划 |
| `mac-agent plan activate` | 激活计划 |
| `mac-agent plan close` | 关闭计划 |
| `mac-agent plan list` | 列出计划 |

### 冲突管理

| 命令 | 作用 |
|------|------|
| `mac-agent record-conflict` | 记录冲突 |
| `mac-agent conflicts` | 列出冲突 |
| `mac-agent resolve-conflict` | 解决冲突 |

### 可观测性

| 命令 | 作用 |
|------|------|
| `mac-agent dashboard` | 项目总览（计划/任务/Agent/冲突/指标） |
| `mac-agent metrics` | 6 项聚合指标 |
| `mac-agent audit` | 按 trace_id 查看审计轨迹 |

### 全局选项

| 选项 | 作用 |
|------|------|
| `--verbose` | 显示 DEBUG 级别输出 |
| `--quiet` | 只显示错误 |

---

## 6. MCP 工具参考（给 AI Agent 用）

| 工具 | 作用 | 副作用 |
|------|------|--------|
| `mac_submit_task` | 提交任务 | 写 |
| `mac_claim_task` | 认领+启动任务（原子操作） | 写 |
| `mac_record_quality_and_complete` | 提交质量证据+自动完成 | 写 |
| `mac_fail_task` | 标记失败 | 写 |
| `mac_save_handoff` | 保存交接记录 | 写 |
| `mac_list_ready_tasks` | 列出可认领任务 | 只读 |
| `mac_review_packet` | 生成审核指引 | 只读 |
| `mac_worker_packet` | 生成工作指引 | 只读 |
| `mac_mark_review_ready` | 提交审核 | 写 |
| `mac_accept_review` | 审核通过 | 写 |
| `mac_reject_review` | 审核驳回 | 写 |
| `mac_expire_stale_tasks` | 过期卡住的任务 | 写 |
| `mac_next_task` | 一键认领+启动+工作指引 | 写 |
| `mac_expire_stale_agents` | 下线超时 Agent | 写 |

MCP Resources（只读）：
- `mac://capabilities` — Agent 能力清单
- `mac://health` — 健康状态

---

## 7. 环境变量配置

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `MAC_REQUIRE_REVIEW` | 启用审核流程 | `false` |
| `MAC_REQUIRE_PATH_CHECK` | 启用路径边界检查 | `false` |
| `MAC_REVIEWER_CAPABILITY` | 审核所需的能力名 | 无（不限制） |
| `MAC_MAX_RETRY_COUNT` | 最大重试次数 | `3` |
| `MAC_AGENT_TIMEOUT` | Agent 心跳超时秒数 | `300` |
| `MAC_PATH_RULES` | 路径规则 `allowed\|forbidden` | 无限制 |

---

## 8. 任务状态机

```
proposed → accepted → running → completed
   |          |           |
   v          v           v
rejected   rejected     failed
                        cancelled

# 当 require_review=True 时：
running → review_ready → completed
                      → rejected
```

---

## 9. Python API 快速参考

```python
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.protocol.messages import (
    AgentCard, AgentCapability, TaskTransfer, TaskPayload,
    HandoffResult, CoordinationPolicy,
)

# 创建 Registry
registry = Registry(SQLiteTaskLedger("mac.db"))

# 带审核策略
registry = Registry(
    SQLiteTaskLedger("mac.db"),
    policy=CoordinationPolicy(require_review=True, reviewer_capability="review_code"),
)

# 注册 Agent
registry.register(AgentCard(
    agent_id="claude",
    name="Claude Code",
    capabilities=[AgentCapability(name="write_code")],
    allowed_paths=["src/**"],
))

# 提交任务
task = registry.submit_task(TaskTransfer(
    task_id="task-1",
    payload=TaskPayload(type="write_code", summary="Implement feature"),
    depends_on=[],  # 上游依赖
    ttl_seconds=3600,  # 1小时超时
))

# 认领+启动
claimed = registry.claim_next_task(agent_id="claude", capability="write_code")
registry.start_task(claimed.task_id, "claude")

# 保存交接
registry.save_handoff_result(HandoffResult(
    task_id="task-1",
    agent_id="claude",
    changed_files=["src/feature.py"],
    risks=["manual test needed"],
))

# 审核流程
registry.mark_review_ready("task-1", agent_id="claude")
registry.accept_review("task-1", reviewer_id="reviewer")  # 或 reject_review

# 恢复卡住的任务
expired = registry.expire_stale_tasks(auto_retry=True)

# 下线超时 Agent
expired_agents = registry.expire_stale_agents(timeout_seconds=300)

# 生成工作指引（给下一个 Agent 看）
packet = registry.prepare_worker_packet("task-2", agent_id="qoder")

# 生成审核指引（给审核人看）
review = registry.prepare_review_packet("task-1")

# 查看指标
from mac.metrics import compute_metrics
metrics = compute_metrics(registry.ledger)
```

---

## 10. HTTP API

```python
from mac.transport.http_ws import create_app
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger

app = create_app(Registry(SQLiteTaskLedger("mac.db")))
# uvicorn app:app --port 8000
```

核心端点：

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/metrics` | 6 项聚合指标 |
| `POST` | `/tasks/expire-stale` | 过期卡住的任务 |
| `POST` | `/agents/expire-stale` | 下线超时 Agent |
| `POST` | `/agents/{id}/next` | 一键认领+工作指引 |
| `GET` | `/tasks/{id}/worker-packet` | 工作指引 |
| `GET` | `/tasks/{id}/review-packet` | 审核指引 |

完整端点列表见 `README.md`。

---

## 11. 示例脚本

```bash
# 单 Agent 交接流程
python examples/local_handoff.py

# 多 Agent 协作计划
python examples/collaboration_plan.py

# 审核流程
python examples/review_lifecycle.py

# 本地 Adapter
python examples/local_runner.py

# 完整 E2E 多 Agent 验证（17 步）
python examples/e2e_multi_agent.py
```

---

*MAC v0.6.0 使用手册 — 2026-07-23*
