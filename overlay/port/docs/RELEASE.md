# 发布手册：把构建产物推到 GitHub Release（需求 7）

> 本文件属于 `port/` 自有命名空间，跟随 fork 走，不与上游 `xiaomipad-8pro-mainline`
> 的任何文件重叠（需求 3）。发布动作**只在 GitHub Actions 上执行**，本地脚本只产出资产。

## 1. 三条发布路径

| 场景 | 怎么做 | 走哪个 workflow |
|---|---|---|
| **正式发版（推荐）** | `git tag -a v0.10.0-port1 -m "..." && git push origin v0.10.0-port1` | `sunuefi-build.yml`：先跑完整构建，再由 `publish` job 自动发布到该 tag |
| **不打 tag，临时发布** | Actions → `SunUEFI build products` → Run workflow → 勾 `publish_release`，`release_tag` 留空则自动生成 `port-<短SHA>` | 同上 |
| **补发 / 重发（不重新编译）** | Actions → `SunUEFI publish release` → Run workflow → 填 `tag`，`run_id` 留空自动取最近一次成功的构建 | `sunuefi-release.yml` |

可选联动：在 `SunUEFI sync upstream` 手动 dispatch 时填 `release_tag_after_sync`，
同步上游成功后会自动打 tag 并推送，从而"同步 → 构建 → 发布"一条龙（**需 `SYNC_TOKEN`**）。

## 2. Release 里到底有什么

`port/tools/port-release-assets.py` 把 CI 的三产物汇总成平铺文件：

| 资产 | 内容 |
|---|---|
| `SunUEFI-Module.zip` | Android 引导助手模块（原 `SunUEFI-Module/` 目录全部文件） |
| `SunUEFI-ESP.zip` | `esp.sparse.img` + 目录级 `SHA256SUMS`（刷 `sunuefi_esp`） |
| `SunUEFI-Root-<target>.zip` | `root-<target>.sparse.img` + 目录级 `SHA256SUMS`（刷 `sunuefi_root`） |
| `manifest.json` | 汇总清单：各 ZIP 的 sha256、源文件、状态、tag / commit / run / 上游基线 |
| `SHA256SUMS` | **全局**校验和，覆盖所有 ZIP 与 `manifest.json` |
| `RELEASE-NOTES.md` | 发布说明：构建信息、产物表、校验和全文、占位警告、刷机红线 |

**为什么必须打成 ZIP？** 三个产物目录里各有一个同名 `SHA256SUMS`，GitHub Release 的
asset 是平铺的，直接上传会互相覆盖。压成同名 ZIP 后每个目录自成一份，解压即得原始产物。
ZIP 用 `STORED`（不压缩）+ 固定时间戳：sparse 镜像本身已稀疏编码，再压缩收益很小却要
花几分钟，且固定时间戳让发布资产可复现。脚本流式写入，不把数 GB 镜像读进内存。

## 3. 权限

- `publish` job 使用 **job 级** `permissions: contents: write, actions: read`；
  其余 job 仍是 `contents: read`，遵循最小权限。
- `GH_TOKEN` 用内置的 `github.token`，**不需要额外 Secret**。
- 只有 `sunuefi-sync-upstream.yml` 的"同步后自动打 tag"需要 `SYNC_TOKEN`（`repo` 权限
  PAT）——GitHub 默认 token 的 push 不会触发别的 workflow（防循环设计）。
- 若把仓库设为只读 `GITHUB_TOKEN`（默认 `contents: read`），需在仓库
  Settings → Actions → General → Workflow permissions 勾 **Read and write permissions**。

## 4. 幂等与覆盖

- 同 tag 的 Release 已存在 → 走 `gh release edit --notes-file` 更新说明 +
  `gh release upload --clobber` 覆盖同名 asset（策略见 `config/release-policy.json` 的
  `clobber: true`）。所以 CI 重跑不会失败。
- 不存在 → `gh release create`，并按 dispatch 输入决定是否 `--draft` / `--prerelease`。
- 建议正式版打**附注 tag**（`git tag -a`），预发布用 `--prerelease` 或 `port-*` 前缀区分。

## 5. 下载后校验

```bash
# 从 Release 下载全部资产到同一目录后
sha256sum -c SHA256SUMS
unzip -l SunUEFI-ESP.zip            # 查看内含 esp.sparse.img 与目录级 SHA256SUMS
cat manifest.json | python3 -m json.tool | head -40
```

`manifest.json` 的 `status` 有两种：

- `RELEASE_ASSETS_READY` —— 三个产物都含真实镜像；
- `RELEASE_ASSETS_CONTAINS_PLACEHOLDERS` —— 至少有一个产物只有占位 `README.txt`。

> ⚠️ **后者只是 CI 骨架验证产物，绝对不能刷机。** 没有 `FULL_BUILD=true` 完成内核 /
> rootfs / UEFI 编译前，Release 里就是占位文件。`RELEASE-NOTES.md` 会自动加上这条警告。
> 真镜像替换方式见 `PORTING-MAP.md` 第 4、6 节。

## 6. 与"随时同步上游"的关系（需求 3）

新增的发布相关文件全部落在两个上游不占用的命名空间：

- `port/tools/port-release-assets.py`、`port/config/release-policy.json`、`port/docs/RELEASE.md`
- `.github/workflows/sunuefi-release.yml`（`sunuefi-` 前缀）、`sunuefi-build.yml` 内新增 job

`sunuefi-build.yml` 与 `sunuefi-sync-upstream.yml` 是**本 fork 自有文件**，本就在
`port_owned_namespaces` 白名单内，改动它们不会造成与 `upstream/main` 的合并冲突。
`port-check-isolation.py` 每次构建/同步仍会证明改动集零上游重叠。

> 两个已核对的 GitHub Actions 官方规则（[Workflow syntax / Triggering a workflow](https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions)）：
> 1. 「**Path filters are not evaluated for pushes of tags**」——`on.push` 里
>    `branches + tags + paths` 同时存在时，tag push 不评估 `paths` 过滤；
> 2. 「**The patterns defined in branches and tags are evaluated against the Git ref's name**」——
>    `branches` 与 `tags` 分别按 ref 类型评估（OR 关系），推送 `v*` tag 不会被
>    `branches: [main]` 过滤掉。
>
> 因此 `git push origin v*` 一定会触发 `sunuefi-build.yml` 的构建与 `publish` job。
> 万一因仓库配置差异未触发，用 `SunUEFI build products` 手动 dispatch 勾
> `publish_release` 兜底，或用 `SunUEFI publish release` 补发。

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `发布被拒绝，缺少必需产物: SunUEFI-Module, SunUEFI-ESP` | `download-artifact` 的 `pattern` 不是 `SunUEFI-*`；或 `build` job 里共享产物上传被 `if` 跳过。检查 Actions run 的 Artifacts 列表 |
| `gh: Resource not accessible by integration` | 仓库 Workflow permissions 是只读；改成 Read and write |
| tag push 后没触发构建 | 见上一节；用手动 dispatch 或 `sunuefi-release.yml` 补发 |
| 同步后没自动构建/发布 | 默认 `GITHUB_TOKEN` 的 push 不触发 workflow；配 `SYNC_TOKEN`（PAT） |
| Release 里同名 asset 被覆盖 | 预期行为（`--clobber`）；不同版本请用不同 tag |
| `gh run download` 报无 artifact | artifact 已过 `retention-days: 14`；重新跑一次构建，或本地用 `port-release-assets.py` 从构建产物直接生成 |
| ZIP 解压后只有 `README.txt` | 占位产物，需 `FULL_BUILD=true` 真实编译（见第 5 节警告） |

## 8. 本地复现（不发版）

```bash
# 假设已有三产物目录（例如 port-build.sh 的输出）
python3 port/tools/port-release-assets.py \
  --artifacts artifacts/downloads/build \
  --out release-assets \
  --tag v0.0.0-local \
  --repo owner/repo --json

cd release-assets && sha256sum -c SHA256SUMS
```

该命令**不联网、不调用 gh、不碰设备**，只写 `release-assets/`；已纳入
`tests/selftest.py` 的离线自检。
