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
| `SunUEFI-Module.zip` | Android 引导助手模块（原 `SunUEFI-Module/` 目录全部文件）。解压后约 **21 MB**：Project-SunUEFI 的完整运行时（`payload/app.bin` 26.8 MB UEFI 应用、`payload/fd.bin`、`bin/piano-boot-repack|request`、`bin/e2fsck|resize2fs`、`webroot/`）**加上** port 层注入的 `/etc` 契约 |
| `SunUEFI-ESP.zip` | `esp.sparse.img` + 目录级 `SHA256SUMS`（刷 `sunuefi_esp`） |
| `SunUEFI-Root-<target>--*.sparse.img.zst[.partN]` | `root-<target>.sparse.img` 实际约 7.9 GiB：先 zstd 压缩到约 3.4 GiB，仍超 Release 单资产 2 GiB 上限才按 1,900,000,000 B 切卷；目录内小文件平铺发布（刷 `sunuefi_root`） |
| `manifest.json` | 汇总清单：各资产的 sha256、源文件、状态、tag / commit / run / 上游基线 |
| `SHA256SUMS` | **全局**校验和，覆盖所有发布资产与 `manifest.json` |
| `RELEASE-NOTES.md` | 发布说明：构建信息、产物表、分卷还原命令、校验和全文、占位警告、刷机红线 |

**为什么 Module 要接 Project-SunUEFI 的基座？** port 仓库自己造不出模块运行时：
`payload/app.bin` 是 EDK2 交叉编译的 UEFI 应用（26.8 MB），`bin/piano-boot-repack`
是 ARM64 native ELF，负责把 payload 无损打进 BOOT 并写 64 B BootRequest。缺了它们，
模块只剩 `payload/etc` 里几份配置（约 8 KB），装上去**不能建分区、不能写 BootRequest、
不能切系统**，`port-switch.sh` 一跑就 command not found。

所以 `port-from-upstream-release.sh` 从 `llfzs/Project-SunUEFI` 的 release 取
`SunUEFI-Module.tar.zst`（约 21 MB），再由 `port/tools/port-module-merge.py` 做
「基座 + 增量」：

- 基座的 `bin/`、`payload/*.bin`、`webroot/`、`customize.sh` 等原样保留（连权限位）
- port 的 `/etc` 配置进 `payload/etc/`，账号（passwd/group/shadow）单独进
  `payload/account/`，由 `piano-port-config.sh` **按用户名合并**——旧的
  `cp -a payload/etc/.` 会把 rootfs 的 40+ 系统账号覆盖成一两行，uid 映射全乱
- `service.sh` 重写：先跑 port 注入，再跑原厂扩容校验（原厂是 `|| exit 0` 短路，
  直接往后追加永远执行不到）
- `module.prop` 沿用基座 `id=piano_sunuefi`，同名覆盖安装，不与原模块并存抢 BOOT

拿不到基座（`--skip-module` 或下载失败）时产物仍会生成，但状态是
`PARTIAL_CONFIG_ONLY`，`port-package-artifacts.py` 会附 `README-PARTIAL.txt` 列出缺什么，
不再像以前那样被误标成 `READY`。基座仓库/tag 可用 workflow 变量覆盖
（`SUNUEFI_MODULE_REPO` / `SUNUEFI_MODULE_TAG`），离线时给
`--module-base <本地 SunUEFI-Module.tar.zst>`。

**为什么小产物要打成 ZIP？** 三个产物目录里各有一个同名 `SHA256SUMS`，GitHub Release 的
asset 是平铺的，直接上传会互相覆盖。压成同名 ZIP 后每个目录自成一份，解压即得原始产物。
ZIP 用 `STORED`（不压缩）+ 固定时间戳：这两个包本来就小（ESP 的 sparse 编码后约 250 MiB），
再压缩省不了多少却要额外耗时，固定时间戳还能让发布资产可复现。脚本流式写入，不把数 GB
镜像读进内存。

**为什么大镜像要先 zstd 再分卷？** GitHub Release 单资产上限 2 GiB，而 root sparse 实际
7.9 GiB。裸切要 5 卷、下载 7.9 GiB；zstd -19 后约 3.4 GiB，2 卷就够，与上游
`piano-ubuntu-<tag>-rootfs.img.zst.part*` 完全同形态，用户手里的还原命令也一致：

```bash
cat SunUEFI-Root-<target>--root-<target>.sparse.img.zst.part* | zstd -d > root-<target>.sparse.img
```

「sparse 已稀疏编码、再压缩没收益」是错的：Android sparse 只剔除全零块，剩下的真实数据
（发行版 rootfs 的文档、可执行文件）仍可压到约 40%。

阈值可用 `--max-asset-bytes` 调整，压缩级别用 `--zstd-level`（默认 19），`--no-compress`
可退回旧的裸切行为；离线自检用极小阈值覆盖该路径，无需真造 GB 级文件。

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

`sunuefi-build.yml` 与 `sunuefi-sync-upstream.yml` 是**本仓库自有文件**，
改动它们不涉及任何上游目录。本仓库不含上游源码副本，也不做 `git merge upstream/main`，
因此不存在合并冲突；`port-check-isolation.py` 每次构建校验的是「port 层自包含 +
顶层未混入上游源码目录 + workflow 前缀正确」。

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

## 9. 产物是不是「能直接刷入的镜像」

### 默认路径产出真实镜像

默认 `source=upstream-release` 会自动消费 `yzddmr6/xiaomipad-8pro-mainline` 最新 Release
里**由上游源码编译发布的成品**（不写死 tag），经 `port/` 层 UEFI 化后产出三个真镜像：

| 产物 | 内容 | 上游资产 / 来源 |
|---|---|---|
| `SunUEFI-ESP` | `esp.sparse.img`：FAT32 ESP（4096 B/扇区）转 Android sparse，含本机固件四件套 `\EFI\Piano\stable\{Image,board.dtb,initramfs,boot.img}`（`boot.img` = Android boot v2，内嵌 DTB） | `piano-ubuntu-<tag>-boot.zip` |
| `SunUEFI-Root-<target>` | `root-<target>.sparse.img`：60 GiB ext4 Android sparse，**原样透传** | `piano-ubuntu-<tag>-rootfs.img.zst.part*` 拼接 zstd 解压 |
| `SunUEFI-Module` | `SunUEFI-Piano-<tag>.zip`：约 21 MB，Project-SunUEFI 完整运行时（UEFI payload / `piano-boot-*` / `e2fsck` / webroot）**+** port 的账号与挂载契约（`payload/etc` + `payload/account`，首启按用户名合并注入） | `llfzs/Project-SunUEFI` release 的 `SunUEFI-Module.tar.zst` + `port/` 合并 |

此时 `manifest.json` 的 `status` = `RELEASE_ASSETS_READY`、发布门禁 `GUARD_CLEAR`，
**不再是强制草稿 + 预发布**。Release 说明会写入本次消费的上游 tag，便于溯源。
本地离线复现（不下载 3.5 GiB 分卷）见 `UPSTREAM-RELEASE.md`。

### 历史问题：占位骨架

早期版本把「从内核源码重编」当主路径（旧 `full_build=false` 默认），公共 runner 拿不到
外部输入时退化为几百字节的占位骨架——三个产物 ZIP 各只有一个 `README.txt`：

```json
"status": "RELEASE_ASSETS_CONTAINS_PLACEHOLDERS",
"placeholder_count": 3,
"warnings": ["存在占位产物，本 Release 仅验证 CI 链路，不可用于刷机"]
```

发布门禁当时把它强制降级为草稿 + 预发布。**该问题的根源是构建输入源选错**，
已通过默认路径改为消费上游 Release 成品解决；门禁作为兜底保留下来。

### 真机边界（务必读）

真实镜像 ≠ 真机一定能启动，仍有三个契约缺口（详见 `PORTING-MAP.md` 第 2、6 节）：

- **G1 挂载契约（当前最高风险）**：上游 A 槽三元组里抽出的 `initramfs` 在 v1.0.0
  只有几十字节，`upstream-adapt.json` 会标 `initramfs_suspicious: true` 并在报告
  `note` 里写「G1 高风险」。刷入前必须确认 init 能挂上 `sunuefi_root`。
- **G2**：内核 `CONFIG_EFI_STUB` / EFI Stub 是否可用。
- **G3**：OS4.0.5 / Android 17 合体 BOOT 头兼容，须先 `piano-boot-repack probe`。

### 高级路径：source=kernel-source

只有显式选 `source=kernel-source` 时才走 `port/build/port-full-build.sh` 从内核源码重编：
从 `kernel/source.json` 读固定 commit → clone/校验内核源码 → 检查交叉工具链 →
跑 `tools/build-kernel.sh` → 生成 4K 对齐 FAT32 `esp.raw` → 写 `port-inputs.env`，
workflow 再把 `--inputs/--esp-image/--root-image/--module-zip` 全量传给 `port-build.sh`。
**任何前置条件缺失都直接失败并给出指引，绝不静默产出占位。**

它需要的外部输入（公共 runner 无法自动获得，放进 `port/inputs/`，可用
`external_inputs_dir` 改目录）：

| 缺的输入 | 来源 |
|---|---|
| 内核源码 | fork 的 `kernel/` 只有 `abi.json`/`source.json`；真实源码在 `source.json` 指向的外部仓库 `linux-sm8750-piano@<commit>` |
| `initramfs.cpio.gz` | `tools/build-initramfs.sh` 需 `BUSYBOX` + `GEN_INIT_CPIO`，须本机构建 |
| `root.ext4` / `root.sparse.img` | `tools/build-rootfs-image.py` 需 sudo、apt 快照、专用 `mke2fs` + qemu-user-static |
| `SunUEFI-Piano-*.zip` | 运行时（UEFI payload + `piano-boot-*`）从 `llfzs/Project-SunUEFI` 的 release 取 `SunUEFI-Module.tar.zst`，不需要本机交叉编译；port 侧只做 `/etc` 合并（`port/tools/port-module-merge.py`） |
| 原厂 DTB overlay / 合体 BOOT / UEFI FD | **只能从你的设备提取**；Android 17 的 BOOT 头须先 `piano-boot-repack probe`（G3） |


### ⚠️ 账号注入的形态限制（需求 5）

`piano/piano` + root=`piano` 由 `port-inject-login.py` 对一个**解出的 rootfs 目录**后处理。
若你提供的是**已打包的 `root.ext4` 镜像**（`--root-image`），port 层无法再往镜像里写
`/etc/shadow`，构建时会打印明确警告。两条路二选一：

- **推荐**：本机构建 rootfs 时，先对目录跑
  `python3 port/tools/port-inject-login.py --rootfs <目录> --profile port/config/port-profile.json`
  和 `port-adapt-rootfs.py`，再用上游 `build-ubuntu-ext4.py` 打包成镜像；
- 或提供 `--inputs/rootfs` 目录形态（此时 `port-build.sh` 会执行注入），但这需要你自行
  完成后续的 ext4 镜像化（`mke2fs -d`，需 root 权限）。

**只给 `root.ext4` 而不预先注入账号 → 镜像里不会有 piano 账号**，`port-build.sh` 的
日志会提醒你。这是镜像形态的固有限制，不是脚本 bug。

即便拿到真镜像，**刷入仍不是单文件线刷**：需要新建 `sunuefi_esp`/`sunuefi_root`、
刷两个 sparse、安装 Android 助手模块、写 BootRequest 切换，首次先 `fastboot boot`
内存试运行；G3 合体 BOOT 未闭环前不能上真机。

## 10. 发布门禁（占位骨架不得冒充正式版）

`port/config/release-policy.json` 的 `placeholder_guard` 默认开启。
`port-release-assets.py` 在检测到占位产物时，除 ZIP/校验和/说明外还会生成
`release-flags.env`（**CI 中间文件，不上传、不计入 `SHA256SUMS`**）：

```
FORCE_DRAFT=1
FORCE_PRERELEASE=1
GUARD_STATUS=GUARD_TRIGGERED
GUARD_MESSAGE='检测到占位产物…'
```

两个 publish job（`sunuefi-build.yml` 的 `publish`、`sunuefi-release.yml`）都会
source 该文件，把 Release 强制创建/编辑为**草稿 + 预发布**，并在 Step Summary 打警告：

| 场景 | 结果 |
|---|---|
| 占位产物，默认 | `gh release create ... --draft --prerelease`；已存在则 `gh release edit --draft --prerelease` |
| 占位产物，手动勾 `allow_placeholder_release=true` | 跳过降级，按你选的 draft/prerelease 设置发布 |
| 真实产物（`RELEASE_ASSETS_READY`） | `GUARD_CLEAR`，flags 全 0，正常发布 |

`manifest.json` 与 `--json` 摘要里都有 `publish_guard` 字段，`RELEASE-NOTES.md`
会写明"发布门禁已生效"。**下载 Release 后先看 `manifest.json` 的 `status`**：
`RELEASE_ASSETS_CONTAINS_PLACEHOLDERS` 就说明只是 CI 骨架，不能刷机。
