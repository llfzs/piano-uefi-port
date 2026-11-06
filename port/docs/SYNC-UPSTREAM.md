# 上游跟随操作手册（需求 3）

目标：**持续吃到 `yzddmr6/xiaomipad-8pro-mainline` 勤快维护的更新，且永不冲突。**

## 独立仓库模型（本仓库）

本仓库是**独立的 UEFI 移植层仓库**：`.github/workflows/` 与 `port/` 直接在仓库根，
**不含上游源码副本**（没有 `kernel/`、`device/`、`ubuntu/`、`tools/` 等目录），
因此**没有 `git merge upstream/main` 这回事，也不存在合并冲突**。

上游源码活水通过「消费上游成品」实现：

```
上游 yzddmr6 源码（他维护，本地编译）
   -> 按 tag 发 Release（boot.zip / rootfs 分卷 / tools.zip / SHA256SUMS.assets）
   -> 本仓库 CI 动态取 releases/latest，下载并校验，UEFI 化
   -> 三产物真镜像 -> Release
```

成品就是他源码编译出来的，且**每次他发新版都会更新**，正好满足「一直跟着更新」。
上游仓库没有 CI（`docs/BUILD.md` 要求 QEMU ARM64 guest、Ubuntu 26.04 ISO、原厂 DTB/DTBO、
签名密钥等，公共 runner 无法复现），他自己也是本地编译完再上传 Release —— 所以
「用他的源码构建」在 GitHub Actions 上的唯一可行形态，就是消费他发布的成品。

## 监视 workflow

`.github/workflows/sunuefi-sync-upstream.yml`（`SunUEFI watch upstream releases`）：

| 项 | 值 |
|---|---|
| 触发 | `schedule`（每周二 02:00 UTC+8）/ `workflow_dispatch` |
| 输入 | `dry_run`、`upstream_tag`（留空取 latest）、`force_rebuild`、`release_tag_after_sync` |
| 动作 | `gh api repos/yzddmr6/xiaomipad-8pro-mainline/releases/latest` 取 tag → 与本仓库已发布 Release 说明里的 `yzddmr6/xiaomipad-8pro-mainline@<tag>` 比对去重 → 未构建过则 `POST repository_dispatch(event_type=upstream-release)` |
| 权限 | `contents: write`；用内置 `github.token` 即可（`repository_dispatch` 是允许触发新 run 的例外事件） |
| 不需要 | `upstream` remote、`SYNC_TOKEN`（除非要自动打 `v*` tag 触发发版）、任何源码合并 |

`sunuefi-build.yml` 监听 `repository_dispatch.types: [upstream-synced, upstream-release]`，
`client_payload.upstream_tag` 会传给 `port-from-upstream-release.sh --tag`。

> 为什么 `upstream-synced` 还留着？兼容外部系统手动派发的事件；本仓库自己的监视
> workflow 只发 `upstream-release`。两者都会触发同一条 `source=upstream-release` 构建。

## 本地等价命令

```bash
# 只检查上游最新 tag 与去重结果（需要 GH_TOKEN，contents:write）
GH_TOKEN=<token> bash port/tools/port-sync-upstream.sh . --check

# 指定版本
GH_TOKEN=<token> bash port/tools/port-sync-upstream.sh . --tag v1.0.0 --check

# 发现新 tag 时派发重建
GH_TOKEN=<token> bash port/tools/port-sync-upstream.sh . --dispatch

# 完全离线自检（不联网）
python3 port/tools/port-check-isolation.py --root . --mode tree --json
bash port/verify-port.sh .
```

`port-check-isolation.py` 在独立仓库模型下校验的是 **port 层自包含**：
protected 文件齐全、配置可解析、workflow 全带 `sunuefi-` 前缀、
**仓库顶层不得混入上游源码目录**（出现 `kernel/` 等即判违规，避免误把源码塞回来）。

## 上游变化对照

| 上游变化 | 本仓库反应 |
|---|---|
| 发新 Release（镜像更新） | 监视 workflow 检测到新 tag → 派发 → 自动重建并（可选）发布 |
| 只推 commit、没发 Release | 不触发 —— 消费的是成品，不是源码 |
| 改了 Release 资产命名/压缩格式 | 下载校验或 `role_of()` 解析会明确失败并报告，需人工适配；**绝不静默产出占位** |
| 想手动指定版本 | `workflow_dispatch` 填 `upstream_tag`，或本地 `--tag` |

## 发布联动

- `sunuefi-build.yml` 的 `publish` job 在 `push v*` tag 或勾 `publish_release` 时，
  把三产物发布到 Release，并在说明里写入本次消费的上游 tag（供去重）。
- `sunuefi-release.yml` 可从任意历史成功 run 补发/重发，不重新编译。
- 监视 workflow 的 `release_tag_after_sync` 可在派发时同时打 `v*` tag 触发自动发版；
  **这一步需要 `SYNC_TOKEN`（repo PAT）**，因为 `GITHUB_TOKEN` 打的 tag 不会触发新 workflow。
- 大镜像（>1.9 GB）由 `port-release-assets.py` 自动分卷，每片 <2 GiB Release 上限，
  发布说明里给出 `cat *.part* > 原文件名` 还原命令。

## 排错

| 现象 | 处理 |
|---|---|
| 监视 workflow 报 `Resource not accessible by integration` | 仓库 Settings → Actions → General → Workflow permissions 勾 **Read and write permissions** |
| 派发了但没触发构建 | 确认 `sunuefi-build.yml` 在默认分支上且 `repository_dispatch.types` 含 `upstream-release` |
| 一直跳过重建 | Release 说明里已有该 tag 记录，用 `force_rebuild=true` 强制 |
| `port-check-isolation.py` 报混入上游源码目录 | 独立仓库不该有 `kernel/` 等目录，删掉或移到仓库外 |
