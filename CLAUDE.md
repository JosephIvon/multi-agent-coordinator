# CLAUDE.md — MAC 开发指南

> 单一规范文档。AI / 人类维护者都读它。
> 本文件只写机器兜不住的协作协议,能在 ruff / mypy / pytest / compileall 里跑出来的约定不重复。

---

## 0. 项目速查

- **定位**:轻量多智能体**协作账本**,不是执行引擎
- **版本**:1.2.0 | **Python**:≥ 3.10 | **License**:MIT
- **核心栈**:Python stdlib + pydantic ≥ 2.0 + 可选 fastapi(http)/ mcp(mcp)
- **存储**:SQLite WAL,单实例强一致;多实例在 Phase 2
- **状态机**:`proposed → accepted → running → completed`(另含 `review_ready` / `rejected` / `failed` / `cancelled` / `superseded`;`review_ready` 仅 `require_review=True` 时启用)
- **测试**:pytest 580+ 用例(详见 [`README.md`](README.md)/ [`docs/SPEC.md`](docs/SPEC.md)),跑 `python -m pytest tests/ -q`

---

## 1. 架构速查(指针化)

```
src/mac/
├── protocol/messages.py   # 协议权威(Pydantic 模型)
├── storage/sqlite.py       # SQLite WAL ledger
├── extensions.py           # 下游 DDL / hook / WebSocket 扩展注册
├── schema_extensions.py    # 下游 SQLite schema 函数式 facade
├── registry.py             # 业务逻辑入口
├── quality/gate.py         # 质量门
├── runner/                 # 本地 adapter(命令/Pytest 模板)
├── transport/http_ws.py    # FastAPI app(仅 http extra)
├── mcp_server.py           # MCP Server(31 tools + 4 resources,仅 mcp extra)
├── metrics.py              # 可观测性聚合(6 指标)
├── events.py               # TaskEventBus
└── cli.py                  # CLI 子命令
```

详细架构 / 端点契约见 [`docs/SPEC.md`](docs/SPEC.md)。要写新功能:**先打开 SPEC.md**。

---

## 2. 编码约定(只列机器兜不住的)

- **类型注解**:公共 API 必须显式返回类型;`X | None` 不用 `Optional[X]`(ruff UP007 已开启)
- **错误**:业务异常抛 `MACError` 子类(见 [`protocol/errors.py`](src/mac/protocol/errors.py));`StatusConflict` 是 SQLite CAS,不算业务错误
- **凭据**:API key / token / 路径前缀**永远不进 SQLite / log / CLI 输出**
- **依赖**:仅 [`pyproject.toml`](pyproject.toml);**不要新建 `requirements.txt`**。MCP 依赖固定在 `>=1.0,<2`，升级到 2.x 必须连同 `mcp_server.py`、MCP 契约测试和文档一起迁移。
- **I/O 边界**:Registry / SQLite / Quality Gate 都是同步接口;CLI / HTTP 是 thin wrapper,不在 wrapper 加业务规则

---

## 3. 测试约定

- **文件**:`tests/test_<subject>.py` 对应 `src/mac/<subject>.py`
- **命名**:`test_<动作>_<对象>_<场景>`,helper 用 `_xxx()` 前缀
- **临时数据库**:`pytest` 的 `tmp_path` fixture(不要硬编码路径)
- **并发**:`ThreadPoolExecutor` + `Barrier` 触发竞争(参考 `tests/test_concurrency.py`)
- **异步**:stdlib `asyncio.run()`(不要 `pytest-asyncio` / `anyio`,避免新依赖)
- **契约**:改 [`protocol/messages.py`](src/mac/protocol/messages.py) schema 必须同 commit 改 [`tests/test_protocol.py`](tests/test_protocol.py)

---

## 4. 协作守则(10 条以内)

**DO**:

1. 改前先想影响面,commit message 写清触动的文件
2. 改 schema / CLI / HTTP / Phase → 同 commit 改 [`docs/SPEC.md`](docs/SPEC.md)
3. 同一错误重试 ≥ 2 次失败 → 停下来排查,不再"重试一次试试"
4. 踩过的坑(耗时 ≥ 30 分钟)写进 [§5 已知陷阱](#5-已知陷阱)

**DON'T**:

1. fallback 掩盖配置错误(SQLite 不可达 ≠ 静默 None;LLM 不可用 ≠ 空诊断)
2. 引入执行引擎(LangGraph / Celery)—— MAC 是账本
3. 拆 `Registry` 单体抽象 → 已有的 thin CLI / HTTP wrapper 足够
4. 反射性加依赖("再装一个试试"先 grep 现有依赖)
5. 改 git 历史(`--force push` / `reset --hard` / `commit --amend` 已 push 的)

---

## 5. 已知陷阱

| ID | 内容 | 状态 | 备注 |
|----|------|------|------|
| K-001 | `tests/test_release_readiness.py` `import tomllib` 必须 `try/except` 兜底 `tomli`(Py 3.10 兼容) | ✅ 已修 | 守 `requires-python = ">=3.10"` |
| K-002 | Windows 离线 `WinError 10051` 不要直接用 `socket.socketpair()`,改 `multiprocessing.Pipe` 或 `asyncio.Queue`;`mcp.client.stdio` 在 Windows + Py 3.10 的 ProactorEventLoop 下无法连接子进程 stdio pipe;Py 3.10 stdio E2E 测试 skipOnWindowsOrPy310;CLI `STATUS_STACK_BUFFER_OVERRUN` (0xC0000409) 间歇性崩溃,已加 `threading.stack_size(8MB)` 兜底 | ✅ 已修 | Linux/macOS Py 3.11+ 正常 |
| K-003 | Python `match` / `X \| None` 是 3.10+ 语法,CI runner 不要锁 3.9 | ⚠️ 预防 | `pyproject.toml` 已守住 |
| K-004 | `starlette` 1.3.x 在 `starlette.testclient` 导入时抛 `StarletteDeprecationWarning`(httpx 0.27 弃用 `app=` 参数,starlette 仍用旧签名) | ✅ 已修 | `pyproject.toml` 的 `dev`/`http` extra 锁 `starlette<1.3`,`filterwarnings` 留作兜底;starlette 完成 httpx2 迁移后可移除两者 |
| K-005 | `tests/test_storage.py::test_audit_trail_lookup_stays_fast_with_many_other_rows` 对冷盘/Windows AV 扫描敏感,单次采样易 flaky | ✅ 已修 | 改为 warmup + 中位数(5 次采样)+ `<200ms` 环境敏感阈值;CI 另加 `--reruns 2` 兜底 |
| K-006 | 未设置上界的 `mcp>=1.0` 会解析到 MCP 2.x；该版本移除了 v1 的 `mcp.server.fastmcp` 导入路径，导致 MCP 测试在收集阶段失败 | ✅ 已修 | v1.2 固定 `mcp>=1.0,<2`；升级 2.x 前须完成服务端与契约测试的迁移 |
| K-007 | `@pytest.mark.asyncio` 需要额外插件；本仓不安装该插件时，异步评分测试会先失败再被重跑，长全量运行表现为停滞 | ✅ 已修 | 测试统一用 `asyncio.run()`；release-readiness 守卫禁止重新引入该 marker |

新踩到的:**同格式追加一行**(就追加,别再开 `KNOWN_ISSUES.md` 文件)。

---

## 6. 文档治理

- **真相源**:`docs/SPEC.md` 是架构 + 端点契约唯一源
- **三同步**:改 API / CLI / HTTP 任何一项 → `SPEC.md` + `CLAUDE.md`(本文件) + 必要时 `README.md` 同 commit
- **不变量**:文档不引用未实现的代码;改 schema 后先跑 `pytest tests/test_protocol.py` 再 commit

---

## 7. 调试 SOP

1. `python -m pytest tests/ -q` — 全过则大概率 OK
2. `python examples/local_handoff.py` — 最小协作流程
3. `python examples/local_runner.py` — adapter loop
4. 搜 `MACError` 子类 — 看上层有没有吞掉异常
5. 查 [§5 已知陷阱](#5-已知陷阱) — 重复问题先看这里

---

## 8. 参考与边界

- **不做**:gRPC / Redis / Postgres / ORM 层 / 执行引擎 / Docker / gitleaks / CI(全部 deferred,见 SPEC §8)
- **AI 工具栈**:Claude Code / Qoder / Trae / Cursor 都能接 MCP server;MAC 提供 CLI 协议,不绑死工具链
- **借鉴**:本文件设计参考过同类多智能体项目的 governance 经验(2026-07-22 调研),采纳最小子集,其余过设计内容未采用

---

## 9. MCP Server 指引

AI 编码工具通过 MCP 接入 MAC,**31 tools + 4 resources**(与 [`README.md`](README.md) / [`docs/SPEC.md`](docs/SPEC.md) 同步):

**任务生命周期(写)**

| Tool | 作用 | 副作用 |
|------|------|--------|
| `mac_submit_task` | 提交任务(完整 TaskTransfer dict) | 写 |
| `mac_claim_task` | 认领 + 启动任务(原子操作) | 写 |
| `mac_next_task` | 认领+启动+输出 worker packet(原子操作) | 写 |
| `mac_record_quality_and_complete` | 提交质量证据 + 闸门通过则自动 complete(旧版,推荐用 mac_done) | 写 |
| `mac_done` | 一键完工:质量证据+交接+完成/审核(自动检测 require_review) | 写 |
| `mac_fail_task` | 标记任务失败 | 写 |
| `mac_cancel_task` | 取消任务(可选原因) | 写 |
| `mac_save_handoff` | 保存结构化交接 | 写 |
| `mac_block_task` | 阻塞任务(可选转交 handoff_to) | 写 |
| `mac_resume_blocked_task` | 解除阻塞并恢复 | 写 |
| `mac_retry_task` | 重试失败任务(可选 fallback agent) | 写 |
| `mac_expire_task_leases` | 过期租约任务 → failed(可选 auto_retry) | 写 |
| `mac_expire_stale_tasks` | 过期 TTL 任务 → failed(可选 auto_retry) | 写 |
| `mac_expire_stale_agents` | 心跳超时 agent → offline | 写 |
| `mac_cleanup_tasks` | 删除终态任务(failed/cancelled/rejected/superseded) | 写 |

**评审(写)**

| Tool | 作用 | 副作用 |
|------|------|--------|
| `mac_mark_review_ready` | running → review_ready(需 require_review=True) | 写 |
| `mac_accept_review` | review_ready → completed | 写 |
| `mac_reject_review` | review_ready → rejected(自动记录冲突) | 写 |

**查询 / 只读**

| Tool | 作用 | 副作用 |
|------|------|--------|
| `mac_list_ready_tasks` | 列出可认领任务 | 只读 |
| `mac_get_task` | 按 ID 取任务详情 | 只读 |
| `mac_list_agents` | 列出 agent(默认 online) | 只读 |
| `mac_worker_packet` | 生成 worker prompt(Markdown,含边界) | 只读 |
| `mac_review_packet` | 生成 reviewer prompt(Markdown) | 只读 |

**打分 / 质量(scorers)**

| Tool | 作用 | 副作用 |
|------|------|--------|
| `mac_list_scorers` | 列出可用 scorer | 只读 |
| `mac_set_scorer` | 设置/清除当前 scorer(传 None 清除) | 写 |
| `mac_test_scorer` | 用样本评估某 scorer | 只读 |

**跨 IDE 上下文(vault / memory)**

| Tool | 作用 | 副作用 |
|------|------|--------|
| `mac_search_vault` | 检索 vault(支持 type/path_prefix 过滤) | 只读 |
| `mac_save_to_vault` | 写入 vault(默认 00-inbox/, status: draft) | 写 |
| `mac_promote_to_knowledge` | 将审核通过的草稿提升到永久知识区 | 写 |
| `mac_remember` | 记一条事实到 ledger | 写 |
| `mac_recall` | 按查询召回事实 | 只读 |

Resources(4): `mac://capabilities`(能力清单), `mac://health`(健康状态), `mac://kanban`(四色看板), `mac://session-context`(跨 IDE 会话快照)。

**工具数同步机制(护栏)**:本表工具数必须与 `src/mac/mcp_server.py` 里 `@mcp.tool()` 装饰器数量、以及 `README.md` 布局块描述保持一致。三者任一漂移会由 `scripts/check_doc_sync.py` 在 CI 拦下(历史曾发生过文档写 `16 tools + 2 resources` 而实际已 31 + 4)。新增/删除 MCP 工具时,**同 commit** 改 `mcp_server.py` + 本表 + `README.md`,然后本地跑 `python scripts/check_doc_sync.py` 自检。

**契约版本同步机制(护栏)**:本 agent 与 mac_coffee 之间的 `MAC_AGENT_CONTRACT_VERSION` 由 `tests/contract_fixtures/mac_coffee_contract.json` 的 `contract_version` 镜像。任一端升级契约版本,**同 release** 必须同步另一端,并跑 `python scripts/check_contract_sync.py --mac-coffee-path <mac_coffee 仓路径>` 确认一致(签名级漂移另由 `test_cross_project_contract.py` 覆盖)。CI 的 `contract-sync` job 仅做 mac-agent 侧 fixture 自检(内网 GitLab 不可从 GitHub Actions 访问 mac_coffee)。

启动方式:

```bash
# Console script
mac-mcp-server

# 或 module 方式
python -m mac.mcp_server

# 指定数据库 (MCP server 和 CLI 都读 MAC_DB_PATH env var)
MAC_DB_PATH=/path/to/mac.db mac-mcp-server
MAC_DB_PATH=/path/to/mac.db mac-agent tasks

# 或 Claude Code 配置
# claude mcp add mac -- mac-mcp-server
```

<!-- MAC:BEGIN managed context entry -->
## Multi-Agent Coordinator (MAC)

This project uses **MAC as the single source of truth** for agent, task, session,
blocker, handoff, and quality state. Do not duplicate project state in IDE rule
files. Read the current worker packet with `mac-agent next` or `mac-agent context`,
and write results back with MAC CLI, MCP, or authenticated HTTP callbacks.

- Ledger: `mac.db` (override with `MAC_DB_PATH`)
- MCP server: `mac-mcp-server`
- Project context cache: `.agent-context/` (generated; never authoritative)
- Rules here are only an entry point; task facts belong in the MAC ledger.
<!-- MAC:END managed context entry -->
