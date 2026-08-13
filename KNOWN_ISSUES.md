# KNOWN_ISSUES.md

Project memory of recurring problems. Read by the `prompt-audit` skill
whenever CLAUDE.md is audited.

## 当前优先修复顺序

| # | Issue | First seen | Last seen | Times | Owner | Status |
|---|-------|------------|-----------|-------|-------|--------|

(Empty table is fine on a fresh repo. New entries go to the top of the
table; resolved entries move to the bottom with `Status: resolved`.)

## How to add an entry

1. Describe the mistake or anti-pattern in one sentence.
2. Cite the commit SHA or session where it first appeared.
3. Mark "Last seen" with the most recent occurrence (update on each
   repeat; do not create a new entry for the same issue).
4. Bump the "Times" counter.
5. If a CLAUDE.md rule exists that should have prevented it, link it
   in the Owner column.

## Entries

### (none yet)

## Resolved

### 1. `mac_save_to_vault` UnboundLocalError in Phase 3

- **Issue**: `mac_save_to_vault` 的 `_do` 闭包中对 `content` 参数重新赋值，
  Python 将其视为局部变量，导致 `UnboundLocalError: local variable 'content'
  referenced before assignment`。
- **Root cause**: 闭包内对外层函数参数赋值时缺少 `nonlocal` 声明。
- **Fix**: 在 `_do` 函数开头添加 `nonlocal content`。
- **Commit**: `8b3bf84` (2026-08-12)
- **Status**: resolved

### 2. `.mcp.json` 被错误清空

- **Issue**: Phase 3 开发期间 `.mcp.json` 的 `mcpServers` 被清空为 `{}`，
  导致 MCP server 无法启动。
- **Root cause**: 编辑误操作。
- **Fix**: `git checkout -- .mcp.json` 恢复配置。
- **Commit**: `8b3bf84` (2026-08-12)
- **Status**: resolved

### 3. Async scoring tests depended on an undeclared pytest plugin

- **Issue**: 9 个评分测试使用 `@pytest.mark.asyncio`，但开发依赖不包含
  `pytest-asyncio`；pytest 将其判为失败并按默认策略重跑，长全量运行看似停滞。
- **Root cause**: 测试违反 `CLAUDE.md` 的 stdlib `asyncio.run()` 约定。
- **Fix**: 改用 `asyncio.run()`，并由 release-readiness 测试递归拦截
  `pytest.mark.asyncio` 的装饰器或模块级用法。
- **Commit**: `2dd847a` (2026-08-13)
- **Status**: resolved
