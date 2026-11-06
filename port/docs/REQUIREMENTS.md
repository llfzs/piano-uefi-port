# 补充需求追踪矩阵（7 项）

> 每一行都能在本 `port/` 子树或 `.github/workflows/sunuefi-*.yml` 里找到可运行实现，
> 并纳入 `verify-port.sh` / `tests/selftest.py` 的离线校验。所有实现遵循一个总原则：
> **独立 port 仓库，不含上游源码副本**，从而保证需求 3 的「一直跟着上游更新、永不冲突」。

| # | 需求 | 实现 | 校验 |
|---|---|---|---|
| 1 | 使用 UEFI 引导双系统 | `port/tools/port-stage-esp.py`（内核四件套→`SUNUEFI-LINUXv2` ESP）、`port-switch.sh`（BootRequest 切换 android/uefi/linux）、`build/port-build.sh` 编排；引导层以 `git subtree` 引入 SunUEFI `uefi/`+`android/module`（见 PORTING-MAP 第 1 节） | selftest 逐字段校验 payload header / cmdline 双兼容参数 |
| 2 | 保留 `sunuefi_esp` / `sunuefi_root` 分区 | `config/port-profile.json` 的 `partitions` 与 `root_policy=LABEL=PIANOROOT`；`port-adapt-rootfs.py` 改写 fstab/root-policy/udev；`port-partition-plan.py` 只新建这两块、**不动 userdata** | selftest 断言分区名/顺序、fstab、udev 保护、`android_data_cleared=false` |
| 3 | 随时同步上游、不与 UEFI 冲突 | `config/upstream-sync.json` 定义两个自有命名空间（`port/`、`.github/workflows/sunuefi-*`）；`port-sync-upstream.sh`（--check/--merge + 命名空间冲突预检）；`port-check-isolation.py` 证明改动集零上游重叠；`.github/workflows/sunuefi-sync-upstream.yml` 定时同步 | isolation selftest（含"上游改动触及命名空间"负例）+ `git merge` dry-run |
| 4 | 三产物包 | `port-package-artifacts.py` 产出 `SunUEFI-Module/`（引导助手 ZIP）、`SunUEFI-ESP/esp.sparse.img`、`SunUEFI-Root-<target>/root-<target>.sparse.img`，含 Android sparse 编码 + manifest.json + SHA256SUMS；`config/distros.json` 定义矩阵 | selftest 校验三目录、sparse magic/chunk、sha256、命名 |
| 5 | 账号 `piano`/`piano`、root=`piano` | `config/port-profile.json` 的 `login`；`port-inject-login.py` 在 rootfs 后处理注入 `/etc/passwd|shadow|group|gshadow` 的 sha512-crypt 哈希 + GDM 自动登录 + sudoers；**不改上游** `build-rootfs-image.py` 的"无账号/root 锁定"校验逻辑（只在其校验之后对产物注入） | selftest 用 `crypt`/`openssl` 反算哈希验证三个密码，并验证幂等 |
| 6 | 新增 GitHub Actions、在 GitHub 上构建 | `.github/workflows/sunuefi-build.yml`（push/repository_dispatch/manual，`ubuntu-24.04-arm`，matrix，三 `upload-artifact` 命名严格对齐 SunUEFI）；`sunuefi-sync-upstream.yml`（schedule + dispatch） | verify 校验 YAML 可被解析、`on/jobs/steps` 结构、三 artifact 名存在 |
| 7 | **构建产物推到 Release** | `sunuefi-build.yml` 新增 `publish` job（push `v*` tag 或 dispatch 勾 `publish_release` 触发，job 级 `contents: write`，`download-artifact` → `gh release create/upload --clobber`）；`sunuefi-release.yml` 手动补发（`gh run download` 复用已有成功 run，不重新编译）；`port/tools/port-release-assets.py` 把三产物目录压成同名 ZIP（规避各目录 `SHA256SUMS` 重名覆盖）并生成全局 `SHA256SUMS` + `manifest.json` + `RELEASE-NOTES.md` + `upload-files.txt`；`config/release-policy.json` 定义 tag/draft/prerelease/上传清单；`docs/RELEASE.md` 为操作手册 | verify 断言 `publish` job、`contents: write`、`tags:`、`gh release`；selftest 端到端生成 Release 资产并校验 ZIP 内容、全局 SHA256、manifest、Notes、缺 ESP 负例 |
| 补强 A | **真实编译接线**（替换旧 echo 占位） | `port/build/port-full-build.sh`：从 `kernel/source.json` 读固定 commit，clone/校验内核源码、检查交叉工具链、跑 `tools/build-kernel.sh`、生成 4K 对齐 FAT32 `esp.raw`、写 `port-inputs.env`；workflow 把 `--inputs/--esp-image/--root-image/--module-zip` 全量传给 `port-build.sh`，并去掉 `|| true` 掩盖 | verify 断言无「示例接线」残留、四个传参齐全、无 `tee ... \|\| true`；selftest 跑 `--dry-run` 校验元数据/源码/输出契约 |
| 补强 B | **发布门禁** | `release-policy.json#placeholder_guard` + `port-release-assets.py` 输出 `release-flags.env`；两个 publish job source 后把占位骨架强制降级为草稿 + 预发布，`allow_placeholder_release` 可显式放行 | verify 断言两个 workflow 都有门禁要素；selftest 覆盖 `GUARD_CLEAR` / `GUARD_TRIGGERED` 与 flags、Notes、上传清单隔离 |

## 三个必须记住的架构决定

1. **"跟着上游、永不冲突" = 独立仓库消费成品，不合并源码。**
   本仓库是独立的 port 层：`.github/workflows/` 与 `port/` 直接在仓库根，
   **不含 `kernel/`、`device/`、`ubuntu/` 等上游源码目录**，因此根本没有
   `git merge upstream/main`、也没有合并冲突。上游源码由 yzddmr6 维护，按 tag 发布
   编译成品；`sunuefi-sync-upstream.yml` 监视 `releases/latest`，发现新 tag 就派发重建。
   `port-check-isolation.py` 校验的是「port 层自包含 + 顶层未混入上游源码目录 +
   workflow 前缀正确」，作为 CI 门禁每次构建都跑。

2. **"别的不动"落到实处 = 适配全部发生在「上游构建产物」上。**
   上游仓库没有 CI（`docs/BUILD.md` 要求 QEMU ARM64 guest、Ubuntu 26.04 ISO、
   原厂 DTB/DTBO、签名密钥等，公共 runner 无法复现），他自己本地编译完再按 tag 发
   Release。所以本仓库只下载并校验这些成品，再做 UEFI 化后处理：
   - 内核/ESP：解 `boot.zip` 的 Android Boot Image → `Image`/initramfs/DTB →
     `SUNUEFI-LINUXv2` ESP，不改任何上游源码或配置。
   - root：`root.sparse.img` 原样透传（已是 Android sparse，可直接 fastboot flash）。
   - 账号/挂载：`port-inject-login.py` + `port-adapt-rootfs.py` 生成的配置打进真实
     Module ZIP，由 `service.sh` 在 Android 侧首次启动注入，不改 60 GiB 镜像本体。
   - 切换/分区：`port-switch.sh` + Module 内 `partition-plan.json`（新建
     `sunuefi_esp`/`sunuefi_root`，不动 userdata）。
   - 文档：全部说明收进 `port/docs/`。

3. **发布层只消费本次构建的 Actions artifacts（需求 7）。**
   `port/tools/port-release-assets.py` 把三产物汇成小产物 ZIP + 大镜像分卷，
   生成全局 `SHA256SUMS`、`manifest.json`、`RELEASE-NOTES.md`；
   `sunuefi-build.yml#publish` 与 `sunuefi-release.yml` 负责上传。
   发布说明里写入本次消费的上游 tag（`yzddmr6/xiaomipad-8pro-mainline@<tag>`），
   既是溯源信息，也是监视 workflow 的去重依据。

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

默认（`source=upstream-release`）在 GitHub Actions 上**能跑绿**的是：隔离校验、离线自检、
拉上游最新 Release 成品并逐个校验 sha256、UEFI 化、**三产物真镜像**打包、artifact 上传，
以及需求 7 的 Release 发布（真镜像 `GUARD_CLEAR`，正式版）。产物是真实镜像文件，
`manifest.json` 写 `RELEASE_ASSETS_READY`。

但**真实镜像 ≠ 真机可启动**：G1 挂载契约（上游 A 槽 initramfs 可能只有几十字节，
`upstream-adapt.json` 会标 `initramfs_suspicious: true`）、G2 EFI Stub、
G3 OS4.0.5 合体 BOOT 头兼容仍须真机验证；本工程不编译 UEFI FD、不写合体 BOOT、
不碰设备。真机刷写验证清单见 `PORTING-MAP.md` 第 6 节。

只有 `source=kernel-source` 才走 `port/build/port-full-build.sh` 从内核源码重编，
届时需自备公共 runner 拿不到的输入：本机构建的 `initramfs.cpio.gz`、`root.ext4`、
`SunUEFI-Piano-*.zip`、原厂 DTB / 合体 BOOT / UEFI FD，以及 rootfs 构建所需的
sudo + apt 快照 + 专用 `mke2fs`。缺任一输入都会**明确失败**，不静默产出占位，
详见 `RELEASE.md` 第 9 节。
