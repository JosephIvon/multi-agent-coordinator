# CLAUDE.md — MAC 开发指南

> 单一规范文档。AI / 人类维护者都读它。
> 本文件只写机器兜不住的协作协议,能在 ruff / mypy / pytest / compileall 里跑出来的约定不重复。

---

## 0. 项目速查

- **定位**:轻量多智能体**协作账本**,不是执行引擎
- **版本**:0.2.0 Alpha | **Python**:≥ 3.10 | **License**:MIT
- **核心栈**:Python stdlib + pydantic ≥ 2.0 + 可选 fastapi(http)/ mcp(mcp)
- **存储**:SQLite WAL,单实例强一致;多实例在 Phase 2
- **状态机**:`proposed → accepted → running → completed`(另含 `rejected` / `failed` / `cancelled` / `superseded`)
- **测试**:pytest ~155 用例,跑 `python -m pytest tests/ -q`

---

## 1. 架构速查(指针化)

```
src/mac/
├── protocol/messages.py   # 协议权威(Pydantic 模型)
├── storage/sqlite.py       # SQLite WAL ledger
├── registry.py             # 业务逻辑入口
├── quality/gate.py         # 质量门
├── runner/                 # 本地 adapter(命令/Pytest 模板)
├── transport/http_ws.py    # FastAPI app(仅 http extra)
├── mcp_server.py           # MCP Server(7 tools + 2 resources,仅 mcp extra)
├── metrics.py              # 可观测性聚合
├── events.py               # TaskEventBus
└── cli.py                  # CLI 子命令
```

详细架构 / 端点契约见 [`docs/SPEC.md`](docs/SPEC.md)。要写新功能:**先打开 SPEC.md**。

---

## 2. 编码约定(只列机器兜不住的)

- **类型注解**:公共 API 必须显式返回类型;`X | None` 不用 `Optional[X]`(ruff UP007 已开启)
- **错误**:业务异常抛 `MACError` 子类(见 [`protocol/errors.py`](src/mac/protocol/errors.py));`StatusConflict` 是 SQLite CAS,不算业务错误
- **凭据**:API key / token / 路径前缀**永远不进 SQLite / log / CLI 输出**
- **依赖**:仅 [`pyproject.toml`](pyproject.toml);**不要新建 `requirements.txt`**
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
| K-002 | Windows 离线 `WinError 10051` 不要直接用 `socket.socketpair()`,改 `multiprocessing.Pipe` 或 `asyncio.Queue` | ⚠️ 预防 | CI 启用时再加回归用例 |
| K-003 | Python `match` / `X \| None` 是 3.10+ 语法,CI runner 不要锁 3.9 | ⚠️ 预防 | `pyproject.toml` 已守住 |

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

AI 编码工具通过 MCP 接入 MAC,7 tools + 2 resources:

| Tool | 作用 | 副作用 |
|------|------|--------|
| `mac_submit_task` | 提交任务(完整 TaskTransfer dict) | 写 |
| `mac_claim_task` | 认领 + 启动任务(原子操作) | 写 |
| `mac_record_quality_and_complete` | 提交质量证据 + 闸门通过则自动 complete | 写 |
| `mac_fail_task` | 标记任务失败 | 写 |
| `mac_save_handoff` | 保存结构化交接 | 写 |
| `mac_list_ready_tasks` | 列出可认领任务 | 只读 |
| `mac_review_packet` | 生成 reviewer prompt(Markdown) | 只读 |

Resources: `mac://capabilities`(能力清单), `mac://health`(健康状态)。

启动方式:

```bash
# Console script
mac-mcp-server

# 或 module 方式
python -m mac.mcp_server

# 或 Claude Code 配置
# claude mcp add mac -- mac-mcp-server
```
