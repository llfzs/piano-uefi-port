# 用上游源码构建 UEFI 镜像包（默认构建路径）

## 一句话

**上游源码是 `yzddmr6/xiaomipad-8pro-mainline`，一直跟着他更新；本仓库是独立的 `port/` UEFI 适配层，
不含上游源码副本、不做 git merge。** CI 直接消费上游按 tag 发布的「由他源码编译好的成品」，
做 UEFI 化后产出三产物真镜像。

```
上游 yzddmr6 源码（他维护，本仓库不含副本）
        │  维护者本地编译（上游仓库没有 CI）
        ▼
上游最新 Release（v1.0.0、v1.0.1 …，按 tag 发布成品）
        │  CI 自动拉最新 tag + 校验 SHA256SUMS.assets
        ▼
port/ 层 UEFI 化（只加不改）
        │
        ├─ ESP   : 从 boot.zip 抽内核/initramfs/DTB -> SUNUEFI-LINUXv2 -> FAT32 -> sparse
        ├─ Root  : 上游 root.sparse.img 原样透传（已是 Android sparse，可直接 fastboot flash）
        └─ Module: 真实 ZIP（piano/piano 账号 + sunuefi_root 挂载契约 + 分区计划 + 切换）
        ▼
SunUEFI-Module / SunUEFI-ESP / SunUEFI-Root-<distro>  →  Release
```

## 为什么不是「在 CI 里从源码重新编译内核」

上游仓库 `.github/` 里只有 issue 模板，**没有构建 workflow**。他的 `docs/BUILD.md` 写得很清楚，
完整编译需要：QEMU ARM64 guest（root 权限）、Ubuntu 26.04 Desktop ISO、squashfs-tools-ng、
AOSP mkbootimg/avbtool、从自己平板提取的原厂 DTB/DTBO 与签名密钥、数十 GiB 磁盘。
公共 GitHub runner 无法提供这些，上游自己也是在**本地**编译完再上传 Release。

所以「用他的源码构建」在 GitHub Actions 上的唯一可行形态，就是消费他发布的最新成品。
这些成品就是他源码编译出来的，且**每次他发新版都会更新**，正好满足「一直跟着更新」。

> 上一版 `port/build/port-full-build.sh`（从内核源码重编 + 手工外部输入目录）保留为
> 高级选项 `source=kernel-source`，供自建 runner 使用，不再是默认路径。

## 自动跟随上游更新

| 上游变化 | 触发方式 |
|---|---|
| 推了新 commit（没发 Release） | 不触发 —— 本仓库消费的是成品，不合并源码 |
| 发了新 Release（镜像更新） | `sunuefi-sync-upstream.yml` 查 `releases/latest`，与本仓库已发布 Release 说明里的基线 tag 比对，未构建过就派发 `upstream-release` 并带上 tag |
| 手动指定版本 | `workflow_dispatch` 填 `upstream_tag`（留空自动取 latest，不写死） |

`sunuefi-build.yml` 的 `source` 默认 `upstream-release`，因此上述任一触发都会**重新拉上游最新成品重建**。
产物里的 `upstream-release.json` / `upstream-adapt.json` 记录了实际使用的 tag、commit 与每个资产 sha256，可追溯。

## 三产物分别来自上游的什么

| 产物 | 上游资产 | UEFI 化处理 |
|---|---|---|
| `SunUEFI-ESP` | `piano-ubuntu-<tag>-boot.zip`：`boot/boot.img`（内核）、`boot/init_boot.img`（initramfs）、`boot/piano-boot.dtb`（完整合并 DTB） | 解 Android Boot Image v2 header → 裸 `Image`（gzip/lz4 自动解压）+ `initramfs.cpio.gz` + DTB → `port-stage-esp.py` 生成 `SUNUEFI-LINUXv2` payload → `mkfs.vfat` 打成 FAT32 → Android sparse |
| `SunUEFI-Root-ubuntu-gnome` | `piano-ubuntu-<tag>-rootfs.img.zst.part*`，拼接后 `zstd -d` 得 `root.sparse.img`（60 GiB ext4 的 sparse） | **原样透传**（已是 Android sparse，不需要重新编码）；分区刷到 `sunuefi_root` |
| `SunUEFI-Module` | `piano-ubuntu-<tag>-tools.zip`（切换工具作参考） | 生成真实 KernelSU/Magisk 模块：`module.prop` + `service.sh` + `payload/etc/*`（账号 / fstab / root-policy / udev）+ 分区计划 + 切换脚本 |

### 账号与挂载契约为什么在 Module 里

上游 `build-rootfs-image.py` 的发布校验强制「无普通用户账号 + root 密码锁定」，靠 GNOME
Initial Setup 首次开机建用户；`fstab` 也是 `PARTLABEL=linux_rootfs` / `linux_data`。
而需求要预置 `piano/piano`、`root=piano`、挂 `sunuefi_root`。

在 CI 里改 60 GiB 的 sparse 镜像需要 `simg2img` + 挂载 + `img2simg`，磁盘与时间代价极高。
因此把 `port-inject-login.py` / `port-adapt-rootfs.py` 生成的配置**打进 Module ZIP 的 `payload/etc/`**，
由 `service.sh` 在 Android 侧首次启动时挂载 `sunuefi_root` 注入（幂等，带 `.sunuefi-firstboot-done` 标记）。
效果相同，且不触碰上游镜像本体。

内核命令行由 ESP 的 `config` 提供 `root=PARTUUID=<sunuefi_root>` 与 `piano.root=...`，
因此首次启动前根分区就能挂上（见 `PORTING-MAP.md` 的 G1）。

## 本地复现（不依赖 GitHub）

```bash
# 1. 从上游最新 Release 构建 UEFI 输入（真下载，需要 ~4GB 带宽/磁盘）
bash port/build/port-from-upstream-release.sh \
    --fork . --inputs /tmp/port-inputs \
    --repo yzddmr6/xiaomipad-8pro-mainline
#   可选 --tag v1.0.0 指定版本；--skip-root 跳过 3.5GB 分卷仅验证 ESP/Module

# 2. 打包三产物
source /tmp/port-inputs/port-inputs.env
bash port/build/port-build.sh --fork . --output /tmp/artifacts \
    --target ubuntu-gnome \
    --esp-image "$ESP_IMAGE" --root-image "$ROOT_IMAGE" --module-zip "$MODULE_ZIP"
```

需要 `zstd`、`mtools`（`mkfs.vfat`/`mcopy`）、`unzip`、`lz4`、Python 3。
CI 的 build job 会自动 `apt-get install`。

### 离线自检（不联网、不下载 3.5GB）

```bash
python3 port/tests/make_upstream_fixture.py /tmp/fixture
bash port/build/port-from-upstream-release.sh \
    --fork . --inputs /tmp/fixture-inputs --fixture /tmp/fixture
```

fixture 按上游 `assets-manifest.json` 的结构合成小型 `boot.zip` / rootfs 分卷 / tools.zip /
`SHA256SUMS.assets`，能完整跑通「解析 → 下载校验 → 拼接解压 → 解 Boot Image → ESP →
Module → FAT32 → 三产物 → 发布门禁」。自检产出应为 `GUARD_CLEAR`（真镜像，非草稿）。

## 仍然需要真机验证的三件事

本链路产出的是**真实镜像文件**，不再是 README 占位，但「能刷上去并引导」仍需你在设备上闭环：

| 编号 | 契约 | 说明 |
|---|---|---|
| G1 | 挂载契约 | 上游 initramfs 认 `root=`，SunUEFI 认 `piano.root=`。ESP `config` 已同时带两者，需真机确认 init 能挂 `sunuefi_root` |
| G2 | EFI Stub / 合体 BOOT | 上游内核由 A 槽 ABL 加载；UEFI 侧走 `SUNUEFI-LINUXv2` payload + RAM loader，需确认上游 `Image` 能被该 loader 加载 |
| G3 | 固件基线 | 上游 v1.0.0 基于 `OS3.0.303.0.WPYCNXM`，你的设备是 `OS4.0.5.0.XPYCNXM`（HyperOS4 / Android 17），合体 BOOT 头兼容未验证 |

详见 `PORTING-MAP.md` 第 6 节。构建成功 ≠ 硬件可启动。
