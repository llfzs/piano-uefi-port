# 移植映射：SunUEFI 引导/切换/分区 → xiaomipad-8pro-mainline

> 目标：**保留**源仓库勤快维护的内核与硬件适配，**替换**"引导 + 切换 + 分区"三层为
> SunUEFI 方案；并满足 6 项补充需求（见 `REQUIREMENTS.md`）。
> 最高原则：**零上游文件修改（patchless，只增不改）**——所有 UEFI 内容只落在
> `port/` 与 `.github/workflows/sunuefi-*.yml`，因此随时 `git merge upstream/main` 不冲突。
> 本文件随 `port/` 子树进入你的 fork，是移植的唯一事实来源。

## 0. 一句话架构

两套项目是**同一台设备、两套启动链**。本 fork = 上游原样目录 + `port/` 后处理层。

| 层 | 源仓库（现状，原样保留） | SunUEFI / 本方案（目标） | 动作 |
|---|---|---|---|
| 内核 | linux-sm8750-piano，产出 `Image`+`dtb` | 从 ESP 加载 `Image.efi`+`piano.dtb` | **保留源码，只重排产物** |
| 引导打包 | `build-persistent-boot.py` 打 Android `boot.img` 三元组 | UEFI FD + `SUNUEFI-LINUXv2` payload | **port/ 内替换** |
| 启动装载 | ABL 从 A 槽加载 boot 三元组 | UEFI linux-loader 从 `sunuefi_esp` 加载 | **port/ 内替换** |
| 切换 | 三元组写回 A 槽（`piano-boot-switch`） | BOOT 记录区写 64B `BootRequest` | **port/ 内替换** |
| 分区 | 重排 `userdata` → `linux_rootfs`+`linux_data` | 新建 `sunuefi_esp`+`sunuefi_root`，**不动 userdata** | **port/ 内替换** |
| 根挂载 | `root=PARTUUID`（linux_rootfs） | `piano.root=` + `root-policy.json` | **对产物后处理** |
| 账号 | 无普通用户 + root 锁定（OOBE 首次创建） | 预置 piano/piano + root=piano + 自动登录 | **对产物后处理** |
| 硬件用户态 | device/、firmware/、mesa/、ubuntu/ | 沿用 | **保留** |

**关键利好**：源仓库 `build-kernel.sh` 直接产出标准 `Image` 和板级 `dtb`，
`prepare-boot-dtb.py` 产出完整合并 FDT，`build-persistent-boot.py` 原生支持
`--root-partuuid`。所以**不需要改一行内核代码**，桥接层只把这些现成产物重新布局。

## 1. 逐文件映射（保留 / 后处理 / 新增 / 停用调用）

### 保留（原样，继续吃上游更新，一个字都不改）
- `kernel/` · `mesa/` · `firmware/` · `device/` · `apps/` · `initramfs/` · `packaging/`
- `ubuntu/`（26.04 根系统配方、GNOME 补丁）
- 上游所有 `tools/*` 与 `docs/*`、顶层 README/LICENSE/NOTICE

### 「不调用」而非「删除」（避免与上游同步冲突）
- `tools/build-persistent-boot.py`（三元组打包）→ 构建走 `port/tools/port-stage-esp.py`
- `tools/piano-switch.py`、`device/boot/piano-boot-switch`、`device/android/piano-switch-ksu/`
  → 切换走 `port/tools/port-switch.sh` + SunUEFI Android 助手模块
- `ubuntu/boot-profile.json`（AVB/三元组指纹）→ 用 `port/config/port-profile.json`
- `docs/INSTALL.md` 的"重排 userdata / 写 A 槽 / 校验 OS3.0.303.0"→ **不改它**，
  安装说明统一看 `port/docs/`，并在 `port/docs/` 显式声明"以本层为准"

> 上一个版本的工程包曾要求手动改这些上游文件（5 个接线点）——**已全部撤销**。
> 原因：每次同步上游都会冲突。现在改为"文件还在，但本构建链不调用它们"。

### 新增（本 `port/` 子树，自包含）
- `config/port-profile.json` —— 分区（`sunuefi_esp`/`sunuefi_root`/`PIANOROOT`）、cmdline、内核 config 要求、**login（piano/piano/root）**、artifacts 定义
- `config/distros.json` —— 发行版矩阵（默认 `ubuntu-gnome`，扩展位对齐 SunUEFI 四发行版）
- `config/upstream-sync.json` —— 上游同步策略、两个自有命名空间、受管路径白名单、冲突策略
- `config/release-policy.json` —— **需求 7**：tag 前缀/触发、draft/prerelease、clobber、Release 上传清单
- `tools/port-stage-esp.py` —— 内核四件套 → ESP 布局 + `SUNUEFI-LINUXv2` payload
- `tools/port-adapt-rootfs.py` —— rootfs 挂载契约（fstab/root-policy/udev）
- `tools/port-inject-login.py` —— **账号 piano/piano + root piano + 自动登录**
- `tools/port-partition-plan.py` —— 分区操作计划（只出计划，不刷机）
- `tools/port-package-artifacts.py` —— **三产物打包（Android sparse + manifest + SHA256SUMS）**
- `tools/port-check-isolation.py` —— **隔离校验（证明零上游修改）**
- `tools/port-sync-upstream.sh` —— **上游同步（--check/--merge + 命名空间冲突预检）**
- `tools/port-switch.sh` —— 切换接线（BootRequest 语义）
- `tools/port-release-assets.py` —— **需求 7**：三产物目录→同名 ZIP + 全局 SHA256SUMS +
  `manifest.json` + `RELEASE-NOTES.md` + `upload-files.txt`（规避各目录 `SHA256SUMS` 重名覆盖）
- `build/port-build.sh` —— CI/本地构建编排总入口
- `tests/selftest.py`（apply 时复制进 `port/tests/`）、`verify-port.sh`
- `docs/*` —— 本文件、REQUIREMENTS、SYNC-UPSTREAM、**RELEASE（发布手册）**、接线清单、subtree 引用清单

### `.github/workflows/`（新增，带 `sunuefi-` 前缀）
- `sunuefi-build.yml` —— push（含 `v*` tag）/dispatch/repository_dispatch，`ubuntu-24.04-arm`，
  matrix，三 `upload-artifact` 命名严格对齐 SunUEFI `build-products.yml`；
  **`publish` job**（需求 7）：`download-artifact` → `port-release-assets.py` →
  `gh release create/upload --clobber`，job 级 `contents: write`
- `sunuefi-release.yml` —— **需求 7**：手动补发/重发，`gh run download` 复用已有成功 run，不重新编译
- `sunuefi-sync-upstream.yml` —— 每周定时 + 手动，合并上游并触发重建；
  可选 `release_tag_after_sync` 同步后自动打 tag 发版

### 以 subtree / submodule 引入（固定 commit，不复制二进制）
- SunUEFI 的 `uefi/`（core/components/handoff/platforms）、`android/module` `android/native`
- 引导工具：`build_android_product_module.py`、`build_boot_request.py`、`build_boot_repack.py`、
  `package_kernel_payload.py`、`package_sparse_images.py`、`install_piano.py`
- `vendor/piano*`（板级 DTB / 原厂 PE）—— **设备专属，取自你自己平板**

## 2. 三个"契约缺口"（真机前必须闭环，是验证动作，不是文件修改）

| ID | 缺口 | 处理 |
|---|---|---|
| **G1** | 源仓库 initramfs 的 `init` 认标准 `root=`；SunUEFI ram-bootstrap 认 `piano.root=` | cmdline 同时带两种参数（已在 port-profile）；真机前确认 initramfs `init` 能按其一挂上 `sunuefi_root` |
| **G2** | 源仓库内核可能未开 `CONFIG_EFI_STUB`（SunUEFI 要求 EFI Stub Image） | 作为**构建期只读校验项**（`kernel_config_requirements`）；确需开启时在你自己的内核分支加，**不写回上游** `kernel/source.json` |
| **G3** | 合体 BOOT 只支持 Boot v4 / raw AArch64 GKI / 96MiB；你的 OS4.0.5 BOOT 头可能不同 | 用 `piano-boot-repack probe` 先只读探测，确认布局再决定是否可无损打包 |

## 3. 版本基线结论

- 源仓库最新 **v0.10.0 仍锁 `OS3.0.303.0.WPYCNXM`（A 槽）/ Android 16**。
- **改成 UEFI 启动后，Linux 从 ESP 加载，不再依赖 Android `vendor_boot` 三元组，
  也不再做 OS3 版本校验** —— 这是本移植相对"直接刷源仓库"的最大价值：理论上解开版本锁。
- 前提是 **G3**：UEFI 要打包进当前（OS4.0.5 / Android 17）的 BOOT 分区。SunUEFI 已带
  `vendor/firmware/hyperos4-*` 素材，但合体 BOOT 对 BOOT 头有硬约束，必须先 probe。
- ⚠️ 源仓库 `NOTICE`/`LICENSES/` 为多许可（MIT + GPL-2.0-only 改编工具 + 各第三方）。
  并入 SunUEFI（含 BSD-2-Clause-Patent 的 bootselect）前，逐文件核对 SPDX 与 REUSE 合规，
  补 `NOTICE`/`LICENSES/`。这些改动放在 `port/` 内或你自己的 LICENSE 提交，不改上游文件。

## 4. 构建接线（零上游修改）

完整流水线见 `build/port-build.sh` 与 `.github/workflows/sunuefi-build.yml`：

```
[上游原样构建]  build-kernel.sh / prepare-boot-dtb.py / build-initramfs.sh / build-rootfs-image.py
      │  Image  piano-boot.dtb  initramfs.cpio.gz  root.ext4/目录
      ▼
[port 后处理]  port-inject-login.py  (piano/piano + root piano)
              port-adapt-rootfs.py  (fstab -> sunuefi_root, udev 保护 Android)
              port-stage-esp.py     (SUNUEFI-LINUXv2 ESP)
      ▼
[三产物]       port-package-artifacts.py
              SunUEFI-Module/  SunUEFI-ESP/  SunUEFI-Root-<target>/
```

CI 里 `FULL_BUILD=true` 才调用上游完整编译链（需内核源码子模块、Ubuntu apt 快照、
专用 `mke2fs`、设备固件）；默认跑 port 层可离线部分，保证 GitHub Actions 能跑绿。

## 5. 推荐实施顺序

1. `./apply-port.sh <fork路径> <SunUEFI路径>` 落地骨架 → `./verify-port.sh <fork路径>` 自检
2. 配 `upstream` remote（见 `SYNC-UPSTREAM.md`），确认 `--check` 能看到上游更新
3. 用 `git subtree` 引入 SunUEFI 引导层（固定 commit），处理许可证/REUSE
4. 闭环 G2：确认内核 EFI Stub，构建一次 `Image`/`dtb`/`initramfs`
5. `port-partition-plan.py` 出计划 → 对照 `install_piano.py` 校验容量
6. `port-build.sh` 生成三产物 → 上传 artifacts → 按第 6 节真机验证

## 6. 真机验证清单（本沙箱无法替代）

- [ ] `piano-boot-repack probe` 只读探测 OS4.0.5 的 BOOT 布局，确认符合 Boot v4/96MiB（G3）
- [ ] 备份原厂 `boot_a` 全分区 + 每个 UFS LUN 的分区表；确认可 `restore` 还原
- [ ] `fastboot boot SunUEFI-UEFI-Fastboot.img` 先**内存试运行**（不写入），确认 UEFI 能起
- [ ] 确认 initramfs 能挂上 `sunuefi_root`（G1），GDM 自动登录 piano 成功
- [ ] 验证登录：用户名 `piano` 密码 `piano`；`su`/`sudo` 与 root 密码 `piano` 可用
- [ ] 144Hz/触摸/USB-C 外显/合盖唤醒逐项验证
- [ ] 切换：`port-switch.sh to-linux / to-android` 往返，确认持久 BootRequest 生效
- [ ] 回退：冷启动菜单能回 Android；`piano-boot-repack restore` 能还原原厂 BOOT
- [ ] 全程电量 ≥ 40%，绝不写 vbmeta/dtbo/`_b`/persist，绝不 `--set-active=b`

## 7. 红线（沿用两仓共同的安全边界）

> 绝不刷 `vbmeta`/`dtbo`；绝不写或激活 B 槽（`--set-active=b`）；不碰 `persist`/modem/固件分区；
> 不重排/格式化 `userdata`（不清除 Android 数据）；标定与设备专属配置只取自本机；
> 合体 BOOT 必须保留无损还原能力。任何写入前先备份并校验 SHA256。
