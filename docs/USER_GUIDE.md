# MAC 使用手册

> Multi-Agent Coordinator v0.7.0 — 让多个 AI 编码工具在同一项目中协作的本地账本

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

# 带 MCP Server（给 AI 工具用，推荐）
pip install "mac-agent[mcp]"

# 带 HTTP 服务
pip install "mac-agent[http]"

# 全部安装
pip install "mac-agent[mcp,http]"
```

---

## 3. AI Agent 工作流（推荐）

这是 MAC 的**主要使用方式**。AI 工具通过 MCP 接入，只需两步：

### 第一步：配置 MCP

```bash
# Claude Code
claude mcp add mac -- mac-mcp-server

# Cursor / 其他支持 MCP 的工具
# 在工具的 MCP 配置中添加：
# command: mac-mcp-server
# args: []
```

### 第二步：干活

AI Agent 的工作循环只有**两个操作**：

| 步骤 | MCP 工具 | 说明 |
|------|----------|------|
| **接活** | `mac_next_task` | 一键：认领 + 启动 + 获取工作指引 |
| **完工** | `mac_done` | 一键：提交质量证据 + 保存交接 + 完成（或提交审核） |

就这样。不需要记状态机，不需要手动敲命令。

### 完整示例

```
AI Agent 进入项目 →

1. mac_next_task(agent_id="claude", capability="write_code")
   → 返回工作指引 Markdown（任务目标、依赖、验收标准）

2. [AI Agent 执行工作...]

3. mac_done(
     task_id="task-1",
     agent_id="claude",
     quality_result={"command": "pytest -q", "status": "passed"},
     changed_files=["src/auth.py", "tests/test_auth.py"],
     risks=["需要手动浏览器测试"]
   )
   → 返回 {"status": "completed", "quality_gate": "passed", "review": false}

4. 回到步骤 1，接下一个任务
```

### 审核流程（自动触发）

如果项目启用了 `require_review=True`，`mac_done` 会自动将任务标记为 `review_ready` 而不是 `completed`：

```
3. mac_done(task_id="task-1", agent_id="claude", ...)
   → 返回 {"status": "review_ready", "quality_gate": "passed", "review": true}

4. 审核人（另一个 AI Agent 或人类）：
   mac_accept_review(task_id="task-1", reviewer_id="reviewer")
   或
   mac_reject_review(task_id="task-1", reviewer_id="reviewer", reason="缺少错误处理")
```

### 质量门未通过

如果质量证据不够，`mac_done` 不会完成任务，而是返回提示：

```
mac_done(task_id="task-1", agent_id="claude",
         quality_result={"command": "lint", "status": "passed"})
→ {"status": "running", "quality_gate": "failed", "reason": "Missing required commands: pytest"}
```

AI Agent 可以继续提交更多质量证据，再次调用 `mac_done`。

---

## 4. 人类 CLI 工作流

人类偶尔需要用 CLI 查看、管理、恢复任务。核心命令：

### 接活 + 完工（两条命令）

```bash
# 接活：一键认领 + 启动 + 看工作指引
mac-agent next --agent-id alice --capability write_code

# 完工：一键提交质量证据 + 交接 + 完成
mac-agent done --task-id task-1 --agent-id alice \
  --quality-command "pytest -q" --quality-status passed \
  --changed-file src/auth.py --risk "需要浏览器测试"
```

### 查看状态

```bash
# 项目总览
mac-agent dashboard

# 查看可认领的任务
mac-agent ready-tasks --capability write_code

# 查看某个任务详情
mac-agent status --task-id task-1
```

### 恢复卡住的任务

```bash
# 过期超时的任务（支持自动重试）
mac-agent expire-stale --auto-retry

# 下线心跳超时的 Agent
mac-agent expire-stale-agents
```

### 审核流程

```bash
# 提交审核（当 require_review=True 时，done 会自动走这步）
mac-agent review-lifecycle mark-ready --task-id task-1 --agent-id alice

# 审核通过
mac-agent review-lifecycle accept --task-id task-1 --reviewer-id reviewer

# 审核驳回
mac-agent review-lifecycle reject --task-id task-1 --reviewer-id reviewer \
  --reason "缺少错误处理"
```

---

## 5. MCP 工具参考（15 个）

| 工具 | 作用 | 副作用 |
|------|------|--------|
| **`mac_next_task`** | 一键认领+启动+工作指引 | 写 |
| **`mac_done`** | 一键完工（质量+交接+完成/审核） | 写 |
| `mac_submit_task` | 提交任务 | 写 |
| `mac_claim_task` | 认领+启动任务 | 写 |
| `mac_record_quality_and_complete` | 提交质量证据+完成（旧版，推荐用 mac_done） | 写 |
| `mac_fail_task` | 标记失败 | 写 |
| `mac_save_handoff` | 保存交接记录 | 写 |
| `mac_list_ready_tasks` | 列出可认领任务 | 只读 |
| `mac_review_packet` | 生成审核指引 | 只读 |
| `mac_worker_packet` | 生成工作指引 | 只读 |
| `mac_mark_review_ready` | 提交审核 | 写 |
| `mac_accept_review` | 审核通过 | 写 |
| `mac_reject_review` | 审核驳回 | 写 |
| `mac_expire_stale_tasks` | 过期卡住的任务 | 写 |
| `mac_expire_stale_agents` | 下线超时 Agent | 写 |

MCP Resources（只读）：
- `mac://capabilities` — Agent 能力清单
- `mac://health` — 健康状态

---

## 6. CLI 命令参考

### 日常使用（3 个命令搞定一切）

| 命令 | 作用 |
|------|------|
| `mac-agent next` | 接活：认领 + 启动 + 工作指引 |
| `mac-agent done` | 完工：质量 + 交接 + 完成 |
| `mac-agent dashboard` | 看状态 |

### 任务管理

| 命令 | 作用 |
|------|------|
| `mac-agent submit` | 提交任务 |
| `mac-agent ready-tasks` | 列出可认领任务 |
| `mac-agent claim` | 认领任务 |
| `mac-agent start` | 启动任务 |
| `mac-agent complete` | 完成任务（需通过质量门） |
| `mac-agent fail` | 标记失败 |
| `mac-agent retry` | 重试失败任务 |
| `mac-agent cancel` | 取消任务 |
| `mac-agent status` | 查看任务状态 |
| `mac-agent tasks` | 列出所有任务 |
| `mac-agent expire-stale` | 过期卡住的任务（--auto-retry） |

### Agent 管理

| 命令 | 作用 |
|------|------|
| `mac-agent register` | 注册 Agent |
| `mac-agent discover` | 按能力搜索 Agent |
| `mac-agent expire-stale-agents` | 下线超时 Agent |

### 审核流程

| 命令 | 作用 |
|------|------|
| `mac-agent review-lifecycle mark-ready` | 提交审核 |
| `mac-agent review-lifecycle accept` | 审核通过 |
| `mac-agent review-lifecycle reject` | 审核驳回 |

### 协作上下文

| 命令 | 作用 |
|------|------|
| `mac-agent handoff` | 保存/查看交接记录 |
| `mac-agent worker-packet` | 工作指引 |
| `mac-agent review-packet` | 审核指引 |

### 质量门

| 命令 | 作用 |
|------|------|
| `mac-agent quality` | 提交质量证据 |
| `mac-agent quality-preview` | 预览质量门 |
| `mac-agent task-readiness` | 预览下一步操作 |
| `mac-agent task-evidence` | 查看全部证据 |

### 计划 / 冲突 / 可观测性

| 命令 | 作用 |
|------|------|
| `mac-agent plan create/activate/close/list` | 管理协作计划 |
| `mac-agent record-conflict` / `conflicts` / `resolve-conflict` | 冲突管理 |
| `mac-agent metrics` | 6 项聚合指标 |
| `mac-agent audit` | 审计轨迹 |

### 全局选项

| 选项 | 作用 |
|------|------|
| `--verbose` | 显示 DEBUG 级别输出 |
| `--quiet` | 只显示错误 |

---

## 7. 环境变量

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `MAC_REQUIRE_REVIEW` | 启用审核流程 | `false` |
| `MAC_REQUIRE_PATH_CHECK` | 启用路径边界检查 | `false` |
| `MAC_REVIEWER_CAPABILITY` | 审核所需的能力名 | 无（不限制） |
| `MAC_MAX_RETRY_COUNT` | 最大重试次数 | `3` |
| `MAC_AGENT_TIMEOUT` | Agent 心跳超时秒数 | `300` |
| `MAC_PATH_RULES` | 路径规则 `allowed|forbidden` | 无限制 |

---

## 8. 任务状态机（不需要记）

`mac_done` 自动处理状态转换，你不需要记这个：

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

registry = Registry(SQLiteTaskLedger("mac.db"))

# 接活
claimed = registry.claim_next_task(agent_id="claude", capability="write_code")
registry.start_task(claimed.task_id, "claude")

# 完工（一键）
result = registry.done(
    "task-1", "claude",
    quality_result={"command": "pytest", "status": "passed"},
    handoff=HandoffResult(
        task_id="task-1", agent_id="claude",
        changed_files=["src/feature.py"],
        risks=["manual test needed"],
    ),
)
# result = {"status": "completed", "quality_gate": "passed", "review": False}
# 或 {"status": "review_ready", "quality_gate": "passed", "review": True}
```

---

## 10. HTTP API

```python
from mac.transport.http_ws import create_app
app = create_app(Registry(SQLiteTaskLedger("mac.db")))
# uvicorn app:app --port 8000
```

核心端点：

| 方法 | 路径 | 作用 |
|------|------|------|
| `POST` | `/tasks/{id}/done` | 一键完工 |
| `POST` | `/agents/{id}/next` | 一键接活 |
| `GET` | `/metrics` | 聚合指标 |
| `POST` | `/tasks/expire-stale` | 过期卡住的任务 |
| `POST` | `/agents/expire-stale` | 下线超时 Agent |

---

*MAC v0.7.0 使用手册 — 2026-07-23*
