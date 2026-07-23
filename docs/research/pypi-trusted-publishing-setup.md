# PyPI Trusted Publishing 配置指南

> 目标：每次 `git tag v* && git push origin v*` 后，GitHub Actions 自动 build + upload 到 PyPI，无需手动 twine。

---

## 前提条件

- PyPI 账号已注册，2FA 已开启
- 项目已至少手动发布过一次（v0.5.0 已发布 ✅）
- GitHub 仓库：`JosephIvon/multi-agent-coordinator`

---

## Step 1：在 PyPI 添加 Trusted Publisher

1. 登录 https://pypi.org/manage/account/publishing/
2. 点击 **"Add a new publisher"**，选择 **"GitHub"**
3. 填写以下信息：

| 字段 | 值 |
|------|-----|
| **PyPI Project Name** | `mac-agent` |
| **Owner** | `JosephIvon` |
| **Repository name** | `multi-agent-coordinator` |
| **Workflow name** | `publish.yml` |
| **Environment name** | *(留空)* |

4. 点击 **"Add"**

> ⚠️ **注意**：PyPI 要求项目已存在才能添加 Trusted Publisher。`mac-agent` 已通过 v0.5.0 手动发布存在，所以可以直接添加。

---

## Step 2：确认 GitHub Actions 权限

检查 `.github/workflows/publish.yml` 中是否包含：

```yaml
permissions:
  id-token: write  # trusted publishing
```

✅ 已包含，无需修改。

---

## Step 3：验证自动发布

下次打 tag 时自动触发：

```bash
# 修改代码 → commit → tag → push
git tag -a v0.7.0 -m "MAC v0.7.0"
git push origin v0.7.0
```

然后去 https://github.com/JosephIvon/multi-agent-coordinator/actions 查看 workflow 运行状态。

---

## 如果自动发布失败

### 常见错误 1：`403 Forbidden`

**原因**：Trusted Publisher 配置的 Owner/Repository/Workflow 名称不匹配。

**排查**：
- 确认 PyPI 上的 Owner 是 `JosephIvon`（大小写敏感）
- 确认 Workflow name 是 `publish.yml`（不是路径，只是文件名）

### 常见错误 2：`Workflow not trusted`

**原因**：GitHub Actions 的 OIDC token 未正确生成。

**排查**：
- 确认 `permissions: id-token: write` 在 workflow 中
- 确认 tag 推送到了正确的仓库（不是 fork）

### 常见错误 3：`Project not found`

**原因**：PyPI 上项目名不匹配。

**排查**：
- 确认 PyPI 项目名是 `mac-agent`（不是 `mac_agent` 或 `multi-agent-coordinator`）

---

## 回退方案

如果 Trusted Publishing 配置有问题，仍可手动发布：

```powershell
# 设置环境变量避免交互输入
$env:TWINE_USERNAME = "__token__"
$env:TWINE_PASSWORD = "pypi-你的token"

python -m build
twine check dist/*
twine upload dist/*
```

---

## 配置完成后

以后发布流程简化为：

```bash
# 1. 改代码 + commit
git commit -m "feat: ..."

# 2. 打 tag
git tag -a vX.Y.Z -m "MAC vX.Y.Z"

# 3. 推送（自动触发 build + test + publish）
git push origin main --tags
```

CI 会自动：test workflow（push to main）+ publish workflow（tag push）→ PyPI 上线。

---

*操作指南：PyPI Trusted Publishing for mac-agent*
