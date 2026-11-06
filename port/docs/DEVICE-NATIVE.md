# 本机固件契约：把上游成品装进现有分区

> 目标：**用上游 yzddmr6 的 rootfs/内核**，但沿用本机已有的 `sunuefi_esp` / `sunuefi_root`，
> 不改 GPT、不动 userdata。

本文件记录的是**在设备上读出来的事实**，与 `PORTING-MAP.md` 里那套
`SUNUEFI-LINUXv2 + Image.efi` 契约不是同一条链。两套混用会直接开不了机。

## 1. 本机固件到底读什么

`\EFI\Piano\stable\` 四件套（`tools/package_release.py` 的 `esp_files` 即此）：

| 文件 | 本机实测 | 说明 |
| --- | --- | --- |
| `Image` | 48 MB，`MZ` 头 | EFI Stub 内核，固件直接加载 |
| `board.dtb` | 1.2 MB，完整合并 FDT | 独立文件，不在 boot.img 里 |
| `initramfs` | 3.1 MB gzip cpio | 早期用户态 |
| `boot.img` | `ANDROID!`，header v2 | **只装 kernel+ramdisk**：`dtb_size=0`、`cmdline` 为空 |

`boot.img` 头参数（从设备读出，重打包时原样沿用）：

```
page_size=4096  kernel_addr=0x10008000  ramdisk_addr=0x11000000  tags_addr=0x10000100
header_version=2  dtb_size=0  cmdline=""
```

固件 cmdline **编译在固件里**，ESP 里没有 cmdline 文件。设备上 `cat /proc/cmdline` 的实测值：

```
console=tty0 loglevel=4 consoleblank=0 fbcon=font:TER16x32
rdinit=/pianoinit piano.root=LABEL=PIANOROOT rootfstype=ext4 rootwait ro
pd_ignore_unused clk_ignore_unused panic=0 iommu.passthrough=1
```

注意这里**没有** `root=`，并且写死了 `ro`——两件事都得在 `/pianoinit` 里处理。

上游 `boot.img` 走的是另一套加载器，cmdline 写在头部（v4）里，可以直接对照：

```
root=PARTLABEL=linux_rootfs rootfstype=ext4 rw rootwait console=ttyMSM0,115200n8
log_buf_len=2M panic=15 msm.separate_gpu_kms=0 piano.boot=persistent
systemd.gpt_auto=0 bootconfig
```

其中 `systemd.gpt_auto=0` 与 `msm.separate_gpu_kms=0` 会被 `/pianoinit` 补进去（见 3.1），
其余属于他们那套加载器的自有开关，本机固件不认。

## 2. 与上游成品的三个硬冲突

| # | 冲突 | 后果 | 处理 |
| --- | --- | --- | --- |
| 1 | 上游 `boot.img` 是 header **v4**（kernel 在 boot.img、ramdisk 在 `init_boot.img`、vendor ramdisk 在 `vendor_boot.img`） | 本机固件只认 v2 四件套 | 解出 kernel/DTB/vendor_ramdisk，按 v2 重新封装 |
| 2 | 上游真正的 initramfs 在 `vendor_boot.img` 的 **vendor_ramdisk**（zstd，解压 136 MB，systemd initrd）；`init_boot.img` 的 ramdisk 只有 **48 字节**（空 cpio） | 直接取 `init_boot` 会拿到空壳，内核 panic | 取 vendor_ramdisk，不用 init_boot |
| 3 | 固件传 `rdinit=/pianoinit` 且只给 `piano.root=`；上游 initramfs 由 systemd 驱动，只认 `root=` | 找不到 `/pianoinit` 直接 panic；即使有，systemd 也不知道挂哪 | 注入 `/pianoinit` shim：翻译 `piano.root=`→`root=`、`ro`→`rw`，再 `exec /init` |

另外 `init_boot`/`vendor_boot` 都是 header v4，解析时要用 v3+ 的头部布局
（`kernel_size@8, ramdisk_size@12, os_version@16, header_size@20, header_version@40, cmdline@44`），
不能套 v0–v2 的 `kernel_addr/ramdisk_addr/page_size` 布局。

### 2.1 换了之后实际会掉什么

| 项 | 上游 v1.0.0 | 本机当前 |
| --- | --- | --- |
| 内核 | `7.2.14-piano56` | `7.3.0-piano-sunuefi-cachy` |
| `board.dtb` | 147 KB | 1.2 MB |
| rootfs | Ubuntu（上游定制） | Debian 13 + GNOME |

两点要知道：

- **内核更老**。7.3.0 上的修复（UCSI 注销崩溃、PD 判定、快充认证、OTG 配置等）不在
  7.2.14 里。要留在上游镜像上又想要这些修复，只能拿本机内核源码重编一份 Image 换进去。
- **DTB 更小**。他们的 `board.dtb` 与他们内核是配套的，优先整对使用；若出现触摸/显示/
  蓝牙异常，第一件事是拿本机的 1.2 MB `board.dtb` 配他们的内核做交叉验证，判断是
  DTB 裁掉了节点还是内核缺驱动。

## 3. 注入的两个东西

### 3.1 `/pianoinit`（cmdline 翻译）

固件 cmdline 不可改，所以只能改「systemd 看到的 cmdline」：把改写后的内容 bind mount 到
`/proc/cmdline`，再 `exec /init`。PID 1 不变，上游 `piano-physical-platform` 与 systemd
流程原样保留。

```
piano.root=LABEL=PIANOROOT  →  root=LABEL=PIANOROOT
ro                          →  rw
```

再补两个上游 cmdline 里有、固件没有的开关（键已存在就不重复加）：

| 参数 | 原因 |
| --- | --- |
| `systemd.gpt_auto=0` | 别让 systemd 按分区类型自动生成挂载单元，fstab 只由我们修正 |
| `msm.separate_gpu_kms=0` | 上游内核的显示路径按这个默认值验证过，缺了可能只有黑屏 |

shim 里**只用 shell 内建语法**，因为上游 initramfs 里：

- 没有 `mountpoint`（`util-linux` 只装了 `mount`）——所以用 `[ -r /proc/cmdline ]` 判断 `/proc`；
- 不保证有 `sed`——所以用 `for` 循环重建 cmdline，而不是 `sed 's/ ro / rw /'`；
- `/bin`、`/sbin`、`/lib` 都是指向 `usr/*` 的符号链接，`#!/bin/sh` 可以解析（`dash` 在 `/usr/bin/dash`）。

### 3.2 rootfs 首次启动修正单元

上游 rootfs 的 fstab 指向它自己的分区方案（`PARTLABEL=linux_rootfs` / `linux_data`），
本机不存在这两块分区，systemd 会因挂载失败拖住 `local-fs.target`。

`piano-root-fixup.service` 挂在 `initrd-root-fs.target`（`After=sysroot.mount`、
`Before=initrd-switch-root.service`），首次启动时：

1. 注释 fstab 里 `linux_rootfs` / `linux_data` 的行（原文件留 `.piano-orig` 备份）；
2. 补出 `/home`；
3. 写 `.piano-firstboot-done` 标记，之后不再改。

## 4. 构建

```bash
python3 port/tools/port-native-esp.py \
  --kernel  <上游 boot.img 解出的 kernel> \
  --dtb     <上游 boot/piano-boot.dtb> \
  --ramdisk <上游 vendor_boot.img 的 vendor_ramdisk> \
  --init    port/tools/native/pianoinit.sh \
  --fixup-script port/tools/native/piano-root-fixup.sh \
  --output  <输出目录> --compress gzip
```

产出目录结构与 ESP 一致，直接复制到 `/boot/efi/EFI/Piano/stable/` 即可。
`--compress zstd` 也可用（内核同样认），gzip 体积更小。

校验点（脚本已内置）：

- 内核必须是 `MZ`（EFI Stub），否则固件加载不了；
- DTB 必须是完整合并 FDT（`totalsize == 文件长度`）；
- `boot.img` 回读的 kernel/ramdisk 必须与输入字节一致；
- 注入项必须落在 `TRAILER!!!` **之前**（否则内核解包到第一个 trailer 就停）；
- cpio 解析必须按 4 字节对齐跳过文件名填充（漏了对齐会让每个文件整体偏移 1–3 字节）。

## 5. 上机流程

前置：ESP 上**只有** `EFI/Piano/stable/` 这四个文件（实测，没有 `EFI/BOOT`），
所以换 ESP 就是换这四个文件，不存在漏改别的引导项的问题。

1. **换 ESP**（在现有 Linux 里跑，需要 root）：

   ```sh
   sh port/tools/native/deploy-esp.sh /path/to/native-esp/EFI/Piano/stable
   ```

   脚本先把整个 `stable/` 目录 `cp -a` 备份到 `/home/piano/esp-backup-<时间戳>`（附
   `SHA256SUMS`），再覆盖，`sync` 后逐文件回读校验。

2. **刷 root**（必须在 Android 侧用原厂 Fastboot，Linux 里刷不了自己挂在 `/` 的分区）：

   ```sh
   adb reboot bootloader
   fastboot -s SERIAL -S 128M flash sunuefi_root root.sparse.img
   fastboot reboot
   ```

3. 回到 Android 后，在引导助手里选 **UEFI + Linux** 启动。

**顺序不能颠倒**：先换 ESP 再刷 root。反过来的话，中间那次重启是「旧内核 + 新 rootfs」
（内核版本对不上，起不来），而「新内核 + 旧 rootfs」还可能进系统但硬件全丢。

**换完 ESP、还没刷 root 的这段窗口里，Linux 起不来**（内核和 rootfs 对不上），但 Android
照常可用，所以设备不会变砖——这也是为什么两件事要在同一次操作里连着做完。

## 6. 回退

- **ESP**：`sh port/tools/native/rollback-esp.sh /home/piano/esp-backup-<时间戳>`
  （需要能进 Linux）。进不了 Linux 的话，只能从 Android 侧 fastboot 刷回完整 ESP 镜像——
  而 `deploy-esp.sh` 只备份了四个文件，没有整块 `dd` 备份，所以**想留整块回退路径的
  话，换 ESP 之前自己 `sudo dd if=/dev/sda35 of=esp-backup.img bs=1M` 存一份**。
- **root**：上游镜像是 Android 稀疏镜像，`fastboot flash` 会覆盖整块分区，
  本机原 Debian（26 GB 已用）**不可逆丢失**，除非事先做过分区镜像。
- 只想试内核不换 rootfs：不可行。`piano.root=` 编译在固件里，改不了指向。

## 7. 与 port-stage-esp.py 的关系

| 契约 | 使用者 | 产物 |
| --- | --- | --- |
| `SUNUEFI-LINUXv2` payload | 另一版加载器（`LinuxRamBoot.c` 读 `\EFI\Piano\stable\boot.img`，magic 校验 144B 头） | `Image.efi` / `piano.dtb` / `initramfs.cpio.gz` / `config` |
| Android boot v2 四件套 | 本机当前固件 | `Image` / `board.dtb` / `initramfs` / `boot.img` |

两者**不能混用**。刷之前先用 `hexdump -C boot.img | head -1` 确认本机固件是哪一套：
`ANDROID!` 走本文件；`SUNUEFI-LINUXv2` 走 `port-stage-esp.py`。
