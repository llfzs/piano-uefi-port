# 补充需求追踪矩阵（7 项）

> 每一行都能在本 `port/` 子树或 `.github/workflows/sunuefi-*.yml` 里找到可运行实现，
> 并纳入 `verify-port.sh` / `tests/selftest.py` 的离线校验。所有实现遵循一个总原则：
> **零上游文件修改（patchless，只增不改）**，从而保证需求 3 的"随时同步不冲突"。

| # | 需求 | 实现 | 校验 |
|---|---|---|---|
| 1 | 使用 UEFI 引导双系统 | `port/tools/port-stage-esp.py`（内核四件套→`SUNUEFI-LINUXv2` ESP）、`port-switch.sh`（BootRequest 切换 android/uefi/linux）、`build/port-build.sh` 编排；引导层以 `git subtree` 引入 SunUEFI `uefi/`+`android/module`（见 PORTING-MAP 第 1 节） | selftest 逐字段校验 payload header / cmdline 双兼容参数 |
| 2 | 保留 `sunuefi_esp` / `sunuefi_root` 分区 | `config/port-profile.json` 的 `partitions` 与 `root_policy=LABEL=PIANOROOT`；`port-adapt-rootfs.py` 改写 fstab/root-policy/udev；`port-partition-plan.py` 只新建这两块、**不动 userdata** | selftest 断言分区名/顺序、fstab、udev 保护、`android_data_cleared=false` |
| 3 | 随时同步上游、不与 UEFI 冲突 | `config/upstream-sync.json` 定义两个自有命名空间（`port/`、`.github/workflows/sunuefi-*`）；`port-sync-upstream.sh`（--check/--merge + 命名空间冲突预检）；`port-check-isolation.py` 证明改动集零上游重叠；`.github/workflows/sunuefi-sync-upstream.yml` 定时同步 | isolation selftest（含"上游改动触及命名空间"负例）+ `git merge` dry-run |
| 4 | 三产物包 | `port-package-artifacts.py` 产出 `SunUEFI-Module/`（引导助手 ZIP）、`SunUEFI-ESP/esp.sparse.img`、`SunUEFI-Root-<target>/root-<target>.sparse.img`，含 Android sparse 编码 + manifest.json + SHA256SUMS；`config/distros.json` 定义矩阵 | selftest 校验三目录、sparse magic/chunk、sha256、命名 |
| 5 | 账号 `piano`/`piano`、root=`piano` | `config/port-profile.json` 的 `login`；`port-inject-login.py` 在 rootfs 后处理注入 `/etc/passwd|shadow|group|gshadow` 的 sha512-crypt 哈希 + GDM 自动登录 + sudoers；**不改上游** `build-rootfs-image.py` 的"无账号/root 锁定"校验逻辑（只在其校验之后对产物注入） | selftest 用 `crypt`/`openssl` 反算哈希验证三个密码，并验证幂等 |
| 6 | 新增 GitHub Actions、在 GitHub 上构建 | `.github/workflows/sunuefi-build.yml`（push/repository_dispatch/manual，`ubuntu-24.04-arm`，matrix，三 `upload-artifact` 命名严格对齐 SunUEFI）；`sunuefi-sync-upstream.yml`（schedule + dispatch） | verify 校验 YAML 可被解析、`on/jobs/steps` 结构、三 artifact 名存在 |
| 7 | **构建产物推到 Release** | `sunuefi-build.yml` 新增 `publish` job（push `v*` tag 或 dispatch 勾 `publish_release` 触发，job 级 `contents: write`，`download-artifact` → `gh release create/upload --clobber`）；`sunuefi-release.yml` 手动补发（`gh run download` 复用已有成功 run，不重新编译）；`port/tools/port-release-assets.py` 把三产物目录压成同名 ZIP（规避各目录 `SHA256SUMS` 重名覆盖）并生成全局 `SHA256SUMS` + `manifest.json` + `RELEASE-NOTES.md` + `upload-files.txt`；`config/release-policy.json` 定义 tag/draft/prerelease/上传清单；`docs/RELEASE.md` 为操作手册 | verify 断言 `publish` job、`contents: write`、`tags:`、`gh release`；selftest 端到端生成 Release 资产并校验 ZIP 内容、全局 SHA256、manifest、Notes、缺 ESP 负例 |

## 三个必须记住的架构决定

1. **"同步不冲突"= 文件集不相交，不是靠 rebase 技巧。**
   上游 665 个文件里没有 `port/`，`.github/` 里也没有任何 workflow（只有 issue/PR 模板）。
   因此本 fork 的所有自有内容放进 `port/` 和带 `sunuefi-` 前缀的 workflow，
   `git merge upstream/main` 在三方合并里天然不会碰到它们。`port-check-isolation.py`
   就是把这个"不相交"变成可执行的门禁（放进 CI，每次构建/同步都跑）。

2. **"别的不动"落到实处 = 适配全部发生在「上游构建产物」上。**
   上一版工程包留了 5 个"人工接线点"要改上游文件（内核 config、root-image.json、
   停用 piano-switch、改 INSTALL.md）——那会在每次同步时冲突。本版把这些全部撤销：
   - 内核/con·fig：不改 `kernel/source.json`，`CONFIG_EFI_STUB` 作为**构建期校验项**
     （`kernel_config_requirements`），由 CI 在拿到产物后只读检查 / 或在你自己的
     fork 内核分支里单独加，**不写回上游目录**。
   - rootfs：`port-inject-login.py` + `port-adapt-rootfs.py` 对解出的 rootfs 目录后处理。
   - 切换/分区：源仓库的三元组轮换脚本**保留不动**（上游要用就用），本方案只是
     "不调用它们"，改由 `port-switch.sh` 与 Android 助手模块负责。
   - 文档：不改上游 `docs/`，全部说明收进 `port/docs/`。

3. **发布层也不碰上游（需求 7 与需求 3 兼容）。**
   Release 相关的新增文件全部落在既有命名空间：`port/tools/port-release-assets.py`、
   `port/config/release-policy.json`、`port/docs/RELEASE.md`，以及 `.github/workflows/sunuefi-release.yml`
   （前缀命名）。`sunuefi-build.yml` / `sunuefi-sync-upstream.yml` 本就是本 fork 自有文件，
   在其中加 job / 输入不会造成 `git merge upstream/main` 冲突。发布只消费本次构建的
   Actions artifacts，不写回任何仓库源码，也不依赖上游仓库的 Release 结构。

## 与 SunUEFI 上游的对齐点（避免日后漂移）

- artifact 名逐字对齐 `.github/workflows/build-products.yml`：`SunUEFI-Module`、
  `SunUEFI-ESP`、`SunUEFI-Root-${{ matrix.target }}`；共享产物只在首个 target 上传。
- Android sparse 格式逐字段对齐 `tools/package_sparse_images.py`（magic `0xed26ff3a`、
  RAW `0xcac1`、FILL `0xcac2`、4K 块、64MiB RAW 上限）。
- 分区名/GPT 标签对齐 `config/release.json`：`sunuefi_esp`/`sunuefi_root`/`PIANOROOT`。
- 账号注入语义对齐 `tools/build_release_rootfs.py` 的 `chpasswd`（本项目把
  `piano123456` 改为 `piano`，并额外设置 root 密码）。
- runner 型号对齐：`ubuntu-24.04-arm`（rootfs/内核需 ARM64 原生或 qemu binfmt）。

## 诚实边界

本工程包在 GitHub Actions 上**一定能跑绿**的是：隔离校验、离线自检、三产物骨架打包
（含占位说明）、artifact 上传，以及需求 7 的 Release 发布（tag/dispatch 触发 publish job，
产出 ZIP + 校验和 + 说明；占位状态会在 `RELEASE-NOTES.md` 里显式警告"不可刷机"）。
**完整内核/rootfs 编译**（`FULL_BUILD=true`）依赖上游
内核源码子模块、Ubuntu apt 快照、专用 `mke2fs`、以及**必须取自你本机的设备专属固件**，
首次建议在 `ubuntu-24.04-arm` 或自建 ARM64 runner 上跑通后再开启。占位产物目录里的
`README.txt` 明确写了"真实镜像从哪来、怎么替换"。真机刷写验证清单见
`PORTING-MAP.md` 第 6 节。
