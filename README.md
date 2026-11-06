# xiaomipad-uefi-port —— 把 SunUEFI 引导移植进 xiaomipad-8pro-mainline

在你 fork 的 [`yzddmr6/xiaomipad-8pro-mainline`](https://github.com/yzddmr6/xiaomipad-8pro-mainline)
上，**用 UEFI 引导双系统**、走 SunUEFI 的分区与切换，同时**随时同步上游勤快维护的内核/硬件适配
且永不冲突**，并在 GitHub Actions 上构建出三个标准产物包。

这是一套**可离线验证的移植层与 CI 骨架**，不是可直接刷入的成品固件。

## 7 项需求如何落地（逐条）

| # | 需求 | 落在哪 |
|---|---|---|
| 1 | UEFI 引导双系统 | `port/tools/port-stage-esp.py`（`SUNUEFI-LINUXv2` ESP）+ `port-switch.sh`（BootRequest 切换）|
| 2 | 保留 `sunuefi_esp` / `sunuefi_root` | `port/config/port-profile.json` + `port-adapt-rootfs.py` + `port-partition-plan.py`，**不动 userdata** |
| 3 | 随时同步上游、不冲突 | **patchless 只增不改**：UEFI 全在 `port/` 与 `.github/workflows/sunuefi-*`；`port-sync-upstream.sh` + `port-check-isolation.py` 把"不相交"变成 CI 门禁 |
| 4 | 三产物包 | `port-package-artifacts.py` → `SunUEFI-Module` / `SunUEFI-ESP` / `SunUEFI-Root-<target>`（Android sparse + manifest + SHA256SUMS）|
| 5 | 账号/密码 `piano`/`piano`、root=`piano` | `port-inject-login.py` 注入 sha512-crypt 哈希 + GDM 自动登录 + sudoers |
| 6 | GitHub Actions 构建 | `.github/workflows/sunuefi-build.yml`（三产物）+ `sunuefi-sync-upstream.yml`（定时同步）|
| 7 | **构建产物推到 Release** | `sunuefi-build.yml` 的 `publish` job（push `v*` tag / 手动勾 `publish_release`）+ `sunuefi-release.yml`（从已有 run 补发）+ `port-release-assets.py`（三产物→同名 ZIP + 全局 SHA256SUMS + manifest + RELEASE-NOTES）|

完整追踪矩阵见 `overlay/port/docs/REQUIREMENTS.md`，同步机制见 `overlay/port/docs/SYNC-UPSTREAM.md`，
发布操作见 `overlay/port/docs/RELEASE.md`。

## 最关键的架构决定：从"改上游"升级为"零上游修改"

上一版工程包留了 5 个"人工接线点"要求改上游文件（内核 config、root-image.json、
停用 piano-switch、改 INSTALL.md）——**那些正是每次同步上游必然冲突的地方**。本版全部撤销：

- 上游 665 个文件里没有 `port/`，`.github/` 里也没有任何 workflow（只有 issue/PR 模板）；
- 因此本 fork 的自有内容只放 `port/` 和带 `sunuefi-` 前缀的 workflow →
  `git merge upstream/main` 三方合并天然不碰它们；
- 所有"适配"改为**对上游构建产物做后处理**（Image/dtb/initrd→ESP、rootfs→注入账号+改挂载、
  产物→三包），不写回上游一行源码；
- `port-check-isolation.py` 在每次构建/同步时证明"改动集零上游重叠"。

## 目录

```
xiaomipad-uefi-port/
├── README.md
├── apply-port.sh         应用到 fork（含 .github/workflows）
├── verify-port.sh        离线校验（结构/语法/YAML/配置/自检/隔离）
├── tests/selftest.py     合成输入真实跑通全部桥接脚本
└── overlay/              ← 复制进 fork 的内容
    ├── .github/workflows/
    │   ├── sunuefi-build.yml          三产物构建 + publish job 发 Release
    │   ├── sunuefi-release.yml        手动补发/重发（不重新编译）
    │   └── sunuefi-sync-upstream.yml  上游定时同步（可选同步后打发布 tag）
    └── port/                         ← 自包含移植层
        ├── config/  port-profile.json · distros.json · upstream-sync.json
        │            release-policy.json（需求 7）
        ├── tools/   stage-esp · adapt-rootfs · inject-login · partition-plan
        │            package-artifacts · check-isolation · sync-upstream · switch
        │            release-assets（需求 7：三产物 → Release 资产）
        ├── build/   port-build.sh     CI/本地构建编排总入口
        ├── docs/    PORTING-MAP · REQUIREMENTS · SYNC-UPSTREAM · RELEASE ·（apply 生成清单）
        └── tests/   （apply 时复制 selftest.py 进来，保持自包含）
```

## 快速开始

```bash
# 1) 离线自检（不需真机/编译/网络）
./verify-port.sh

# 2) 应用到你 fork 的 mainline 工作树
./apply-port.sh ~/work/xiaomipad-8pro-mainline ~/work/Project-SunUEFI

# 3) 复检落地 + 隔离校验
./verify-port.sh ~/work/xiaomipad-8pro-mainline

# 4) 配置上游同步（一次性）+ 查更新
cd ~/work/xiaomipad-8pro-mainline
bash port/tools/port-sync-upstream.sh . --init
bash port/tools/port-sync-upstream.sh . --check

# 5) 提交、push 到 GitHub → sunuefi-build.yml 自动产出三产物
git add port/ .github/workflows/sunuefi-*.yml && git commit -m "port: SunUEFI UEFI boot layer"
git push origin main

# 6) 发 Release（需求 7）：tag push 会先构建、再由 publish job 自动发布
git tag -a v0.10.0-port1 -m "SunUEFI port1" && git push origin v0.10.0-port1
#    其他方式：Actions → SunUEFI build products → 勾 publish_release；
#              或 → SunUEFI publish release → 填 tag（从已有 run 补发，不重编译）
```

GitHub 上：Actions → `SunUEFI build products` 下载 `SunUEFI-Module`、`SunUEFI-ESP`、
`SunUEFI-Root-ubuntu-gnome`；打 `v*` tag（或勾 `publish_release`）后，
**Releases 页面**自动出现 `SunUEFI-Module.zip` / `SunUEFI-ESP.zip` /
`SunUEFI-Root-*.zip` + `manifest.json` + `SHA256SUMS` + `RELEASE-NOTES.md`。
`SunUEFI sync upstream` 每周定时把上游更新合进来并触发重建（想要"同步完立刻重建
并自动发版"，在 Secrets 配 `SYNC_TOKEN` 并填 `release_tag_after_sync`）。
发布权限、补发流程与排错见 `overlay/port/docs/RELEASE.md`。

## 三个必须自己闭环的缺口（详见 PORTING-MAP 第 2 节）

- **G1 挂载契约**：源仓库 initramfs 认 `root=`、SunUEFI 认 `piano.root=`；cmdline 已带双参数，
  真机前确认能挂上 `sunuefi_root`。
- **G2 内核配置**：`CONFIG_EFI_STUB=y` 作为构建期只读校验项；确需开启在你自己的内核分支加。
- **G3 合体 BOOT（最关键）**：SunUEFI 合体 BOOT 只认 Boot v4 / raw GKI / 96MiB；
  你的 OS4.0.5 / Android 17 的 BOOT 头须先 `piano-boot-repack probe` 只读探测，兼容才能写入。
  这也是"解开版本锁"能否成立的前提。

## 诚实边界

**在 GitHub Actions 上一定能跑绿**：隔离校验、离线自检、三产物骨架打包（含占位说明）、
artifact 上传、以及需求 7 的 Release 发布（`publish` job 用 `gh release create/upload`，
占位产物会在 `RELEASE-NOTES.md` 里明确标注"不可刷机"）。
**完整内核/rootfs 编译**（`FULL_BUILD=true`）依赖上游内核源码子模块、Ubuntu apt
快照、专用 `mke2fs` 与**必须取自你本机的设备专属固件**，首次建议在 `ubuntu-24.04-arm` 或自建
ARM64 runner 上跑通后再开启。编译内核/UEFI FD、生成真实 ESP 与 sparse 镜像、合体 BOOT 写入、
真机启动/切换/回退，按 `PORTING-MAP.md` 第 6 节清单执行。所有脚本只写普通文件，**不碰任何设备**。

> ⚠️ 刷机有变砖风险。首次务必 `fastboot boot` 内存试运行、备份原厂 BOOT 并确认可还原、
> 电量 ≥ 40%，遵守 PORTING-MAP 第 7 节全部红线（不刷 vbmeta/dtbo、不碰 B 槽、不格式化 userdata）。

另：源仓库 `NOTICE`/`LICENSES/` 为多许可（MIT + GPL-2.0-only 改编工具 + 第三方），并入
SunUEFI（含 BSD-2-Clause-Patent 组件）前逐文件核对 SPDX/REUSE 合规。
