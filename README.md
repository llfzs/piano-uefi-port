# piano-uefi-port —— 用上游成品构建 SunUEFI 三产物镜像包

独立的 UEFI 移植层仓库：消费 [`yzddmr6/xiaomipad-8pro-mainline`](https://github.com/yzddmr6/xiaomipad-8pro-mainline)
**最新 Release 里由他源码编译发布的成品**，在 GitHub Actions 上做 UEFI 化，产出可刷入的
`SunUEFI-Module` / `SunUEFI-ESP` / `SunUEFI-Root-<发行版>` 三产物，并发布到 GitHub Release。

> **这个仓库不含上游源码副本，也不做 `git merge upstream/main`。**
> 上游源码由 yzddmr6 自己维护、勤快更新；他本地编译后按 tag 发 Release，
> 本仓库的 CI 动态取 `releases/latest`，校验 `SHA256SUMS.assets` 后 UEFI 化。
> 他一发新版，监视 workflow 自动派发重建 —— 这就是「一直跟着上游更新」的落地方式。

## 一句话架构

```
上游 yzddmr6 源码（他维护，本地编译，按 tag 发 Release）
        │  CI 动态取 releases/latest，不写死 tag
        ▼
上游 Release 成品：boot.zip / rootfs.img.zst.part* / tools.zip / SHA256SUMS.assets
        │  下载 + 逐个校验 sha256
        ▼
本仓库 port/ 层（只做后处理，不改上游任何东西）
        ├─ ESP   : 解 Android Boot Image -> Image/initramfs/DTB -> SUNUEFI-LINUXv2 -> FAT32 -> sparse
        ├─ Root  : root.sparse.img 原样透传（已是 Android sparse，可直接 fastboot flash）
        └─ Module: 真实 ZIP（piano/piano 账号 + sunuefi_root 挂载契约 + 分区计划 + 切换）
        ▼
GitHub Release：三产物 + manifest.json + 全局 SHA256SUMS + RELEASE-NOTES.md
```

## 7 项需求如何落地

| # | 需求 | 落在哪 |
|---|---|---|
| 1 | UEFI 引导双系统 | `port/tools/port-stage-esp.py`（`SUNUEFI-LINUXv2` ESP）+ `port-switch.sh`（BootRequest 切换） |
| 2 | 保留 `sunuefi_esp` / `sunuefi_root` | `port/config/port-profile.json` + `port-adapt-rootfs.py` + `port-partition-plan.py`，**不动 userdata** |
| 3 | 跟着上游更新、不冲突 | **独立仓库，无源码重叠**：`sunuefi-sync-upstream.yml` 监视上游 `releases/latest`，发现新 tag 派发 `upstream-release` 事件 |
| 4 | 三产物包 | `port-package-artifacts.py` → 三个标准命名产物（Android sparse + manifest + SHA256SUMS） |
| 5 | 账号/密码 `piano`/`piano`、root=`piano` | `port-inject-login.py` 生成的配置打进 Module ZIP，首次启动注入 |
| 6 | GitHub Actions 构建 | `.github/workflows/sunuefi-build.yml`（默认 `source=upstream-release`） |
| 7 | 构建产物推到 Release | `sunuefi-build.yml#publish` + `sunuefi-release.yml`（补发）+ `port-release-assets.py`（大镜像自动分卷）+ 发布门禁 |

完整追踪矩阵见 `port/docs/REQUIREMENTS.md`，上游跟随机制见 `port/docs/UPSTREAM-RELEASE.md`，
发布操作见 `port/docs/RELEASE.md`，真机刷写边界见 `port/docs/PORTING-MAP.md` 第 6 节。

## 目录结构（扁平，直接就是最终仓库）

```
piano-uefi-port/
├── .github/workflows/
│   ├── sunuefi-build.yml           # 主构建 + 发布（默认 source=upstream-release）
│   ├── sunuefi-release.yml         # 从已有 run 补发 Release，不重编译
│   └── sunuefi-sync-upstream.yml   # 监视上游最新 Release 并派发重建
├── port/
│   ├── MANIFEST.json               # 架构与需求追踪
│   ├── verify-port.sh              # 离线全量自检（结构/语法/契约/发布门禁）
│   ├── build/                      # 构建编排
│   │   ├── port-from-upstream-release.sh   # 默认：拉上游成品 -> UEFI 三产物真镜像
│   │   ├── port-build.sh                   # 三产物组装总入口
│   │   └── port-full-build.sh              # 高级：从源码重编（需自备上游源码）
│   ├── config/                     # 分区/发行版/发布策略/上游跟随
│   ├── tools/                      # UEFI 化、账号、分区、打包、门禁、校验
│   ├── tests/                      # 离线自检 + 上游 Release fixture 生成器
│   ├── docs/                       # 全部文档
│   └── inputs/                     # 外部输入（.gitignore 忽略大镜像）
├── README.md
└── .gitignore
```

没有 `overlay/`、没有 `apply-port.sh` —— workflows 和 `port/` 直接在仓库根，clone 下来即可用。

## 快速开始

### 在 GitHub 上构建并发布正式版

```bash
# 方式 1：打 tag（自动触发构建 + 发布）
git tag -a v0.10.0-port1 -m "SunUEFI port1" && git push origin v0.10.0-port1

# 方式 2：Actions 手动跑
# Actions -> SunUEFI build products -> Run workflow
#   source=upstream-release（默认）
#   publish_release=true ✅
#   release_tag=v0.10.0-port1
#   draft=false / prerelease=false / allow_placeholder_release=false

# 方式 3：命令行
gh workflow run sunuefi-build.yml --ref main \
  -f source=upstream-release -f upstream_tag= \
  -f publish_release=true -f release_tag=v0.10.0-port2 \
  -f draft=false -f prerelease=false -f skip_root=false
```

判定是不是**真正式版**（三者同时成立）：`manifest.json` 的
`status=RELEASE_ASSETS_READY`；Release 页面 draft/预发布均为否；资产里有
`esp.sparse.img`、`root-*.sparse.img.part*`、`SunUEFI-Piano-*.zip` 而不是 `README.txt`。
`skip_root=true` 会被发布门禁拦下。

### 本地离线自检（不联网、不下载 GB 级资产）

```bash
bash port/verify-port.sh              # 结构 + 语法 + YAML + 全链路自检
python3 port/tests/selftest.py .      # 合成输入跑通 ESP/账号/三产物/发布门禁

# 上游 Release 适配器离线端到端（小型 fixture）
python3 port/tests/make_upstream_fixture.py /tmp/fixture
bash port/build/port-from-upstream-release.sh \
    --fork . --inputs /tmp/fixture-inputs --fixture /tmp/fixture
```

### 本地复现完整构建（需要约 4 GB 带宽/磁盘）

```bash
bash port/build/port-from-upstream-release.sh --fork . --inputs /tmp/port-inputs \
    --repo yzddmr6/xiaomipad-8pro-mainline     # 可加 --tag v1.0.0
source /tmp/port-inputs/port-inputs.env
bash port/build/port-build.sh --fork . --output /tmp/artifacts --target ubuntu-gnome \
    --esp-image "$ESP_IMAGE" --root-image "$ROOT_IMAGE" --module-zip "$MODULE_ZIP"
```

依赖：`zstd`、`mtools`（`mkfs.vfat`/`mcopy`）、`unzip`、`lz4`、`python3`；CI 会自动 apt 安装。

## 上游一发新版会怎样

| 上游变化 | 本仓库反应 |
|---|---|
| 发新 Release（镜像更新） | `sunuefi-sync-upstream.yml` 每周定时查 `releases/latest`，与本仓库 Release 说明里的 `<上游仓库>@<tag>` 比对，没构建过就派发 `upstream-release` 事件自动重建 |
| 推了新 commit 但没发 Release | 不影响 —— 本仓库消费的是成品 Release，不合并源码 |
| 想手动指定版本 | `workflow_dispatch` 填 `upstream_tag`（留空自动取 latest） |

`sunuefi-build.yml` 的 `source` 默认 `upstream-release`，上述任一触发都会重新拉上游最新成品重建。
产物里的 `upstream-release.json` / `upstream-adapt.json` 记录实际使用的 tag、commit 与每个资产 sha256，可追溯。

## 真机边界（务必读）

真实镜像 ≠ 真机一定能启动，仍有三个契约缺口（详见 `port/docs/PORTING-MAP.md` 第 2、6 节）：

- **G1 挂载契约（当前最高风险）**：上游 v1.0.0 的 A 槽三元组里抽出的 `initramfs` 只有几十字节，
  `upstream-adapt.json` 会标 `initramfs_suspicious: true`。刷入前必须确认 init 能挂上 `sunuefi_root`。
- **G2**：内核 `CONFIG_EFI_STUB` / EFI Stub 是否可用。
- **G3**：OS4.0.5 / Android 17 合体 BOOT 头兼容，须先 `piano-boot-repack probe`。

刷机红线：先备份原厂 `boot_a` 全分区和分区表；绝不刷 `vbmeta`/`dtbo`/`*_b`/`persist`/`modem`；
绝不 `--set-active=b`；不重排/格式化 `userdata`；首次先 `fastboot boot` 内存试运行。

## 参考

- 上游源码与成品：[yzddmr6/xiaomipad-8pro-mainline](https://github.com/yzddmr6/xiaomipad-8pro-mainline)
- UEFI 契约/工具参考：[llfzs/Project-SunUEFI](https://github.com/llfzs/Project-SunUEFI)（非构建输入源）
