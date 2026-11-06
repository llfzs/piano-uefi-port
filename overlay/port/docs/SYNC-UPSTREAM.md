# 上游同步操作手册（需求 3）

目标：**随时把 `yzddmr6/xiaomipad-8pro-mainline` 的更新合进本 fork，持续吃到它勤快维护的
内核/硬件适配，同时 UEFI 移植层永不被覆盖、永不冲突。**

## 为什么不冲突（一句话）

本 fork 相对上游只多了两类文件，且这两类文件上游**从不占用**：

```
port/                              # 所有 UEFI 引导/切换/分区/账号/打包逻辑
.github/workflows/sunuefi-*.yml    # 两个构建/同步 workflow（带 sunuefi- 前缀）
```

上游受管的是 `kernel/ device/ ubuntu/ tools/ firmware/ mesa/ packaging/ initramfs/ apps/
docs/` 及顶层 README/LICENSE 等（完整白名单见 `config/upstream-sync.json`）。
`git merge upstream/main` 的三方合并里，上游只改它自己的目录，本 fork 独有的 `port/`
与 `sunuefi-*.yml` 原样保留 —— **文件集不相交，就没有冲突面**。

## 一次性配置（fork 建好后做一次）

```bash
git remote add upstream https://github.com/yzddmr6/xiaomipad-8pro-mainline.git
# 或让脚本代劳：
bash port/tools/port-sync-upstream.sh . --init
```

## 日常同步（三选一）

```bash
# 1) 只看有没有更新、跑隔离校验，不合并
bash port/tools/port-sync-upstream.sh . --check

# 2) 合并上游 + 合并后隔离校验 + 离线自检
bash port/tools/port-sync-upstream.sh . --merge
git push origin main     # 触发 sunuefi-build.yml 重建三产物

# 3) 全自动：GitHub 上 .github/workflows/sunuefi-sync-upstream.yml
#    每周二定时 / 手动 dispatch，dry_run 可只检查不合并
```

`--merge` 内部做了 4 道保护：工作树干净检查 → 命名空间冲突预检（上游若竟触及
`port/` 或 `sunuefi-*.yml` 立即中止）→ `git merge --no-edit` → 合并后
`port-check-isolation.py --mode git` + `verify-port.sh` 自检。任何一步不过都会中止，
**绝不自动覆盖你的移植层**。

## 几个"如果"

- **如果上游某天新增了一个 `port/` 目录或同名 `sunuefi-*.yml`**（概率极低）：
  sync 脚本的 `precheck_conflicts` 会中止并报告，需人工把本 fork 对应内容改名或合并。
  这就是 `upstream-sync.json` 里 `conflict_policy: 绝不自动覆盖` 的含义。
- **如果上游改了构建脚本接口**（比如 `build-kernel.sh` 产物路径变了）：这**不会**造成
  git 冲突（那是上游自己的文件），但可能让 `port-stage-esp.py` 的输入路径失效。
  处理方式在 `port-build.sh` 的输入参数里改，仍不改上游。CI 的 `FULL_BUILD` 步骤
  是这块的接线示例。
- **如果上游把某功能合进了内核、和你 port 的补丁重复**：本方案没有"往上游源码打补丁"
  这一动作（所有适配都是对产物后处理 + 只读校验），所以不存在 `git am` 冲突或
  "静默双重实现"。这正是前几轮讨论的"优先搬结果、不搬补丁文件"的落地。
- **如果想把 port 的改进回馈上游**：`port/` 是独立子树，可以单独开 PR，或把
  `port/docs/` 的说明提交给上游讨论，不影响同步链路。

## 触发重建

合并后 push 到 `main`，`sunuefi-build.yml` 因 `paths` 覆盖上游目录会自动触发；
sync workflow 若配置了 `SYNC_TOKEN`（PAT），还会额外派发 `repository_dispatch:
upstream-synced` 主动触发。**注意**：GitHub 默认 `GITHUB_TOKEN` 的 push 不会再触发
别的 workflow（防循环），所以想要"同步完立刻自动构建并拿到新三产物"，请在仓库
Secrets 里配一个 `SYNC_TOKEN`（`repo` 权限 PAT）。没配也没关系，push 本身已能触发
`sunuefi-build.yml` 的 `push.paths`。

## 同步完直接发版（需求 7）

三种做法，任选其一（详见 `RELEASE.md`）：

```bash
# A) 手工打 tag（最常用）——tag push 会先构建、再由 publish job 自动发 Release
git tag -a v0.10.0-port2 -m "sync upstream + release" && git push origin v0.10.0-port2

# B) 同步 workflow 里填 release_tag_after_sync：合并成功后自动打 tag 并推送
#    Actions → SunUEFI sync upstream → Run workflow → release_tag_after_sync=v0.10.0-port2
#    （必须配 SYNC_TOKEN，GITHUB_TOKEN 的 push 不会触发别的 workflow）

# C) 构建已跑完、只想补发：Actions → SunUEFI publish release → 填 tag 即可
```

`publish` job 会发布 `SunUEFI-Module.zip` / `SunUEFI-ESP.zip` /
`SunUEFI-Root-*.zip` + `manifest.json` + `SHA256SUMS` + `RELEASE-NOTES.md`，
并在说明里记录本次上游基线。同 tag 重跑用 `--clobber` 覆盖，不会失败。

## 校验同步正确性

```bash
# 证明：本 fork 相对 upstream/main 的改动集，全部落在 port/ 与 sunuefi-*.yml
python3 port/tools/port-check-isolation.py --root . --mode git --json
# status 应为 OK，changed_count 即本 fork 的移植提交数，violations 为空
```
