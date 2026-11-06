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

固件 cmdline **编译在固件里**，ESP 里没有 cmdline 文件：

```
rdinit=/pianoinit piano.root=LABEL=PIANOROOT rootfstype=ext4 rootwait ro ...
```

## 2. 与上游成品的三个硬冲突

| # | 冲突 | 后果 | 处理 |
| --- | --- | --- | --- |
| 1 | 上游 `boot.img` 是 header **v4**（kernel 在 boot.img、ramdisk 在 `init_boot.img`、vendor ramdisk 在 `vendor_boot.img`） | 本机固件只认 v2 四件套 | 解出 kernel/DTB/vendor_ramdisk，按 v2 重新封装 |
| 2 | 上游真正的 initramfs 在 `vendor_boot.img` 的 **vendor_ramdisk**（zstd，解压 136 MB，systemd initrd）；`init_boot.img` 的 ramdisk 只有 **48 字节**（空 cpio） | 直接取 `init_boot` 会拿到空壳，内核 panic | 取 vendor_ramdisk，不用 init_boot |
| 3 | 固件传 `rdinit=/pianoinit` 且只给 `piano.root=`；上游 initramfs 由 systemd 驱动，只认 `root=` | 找不到 `/pianoinit` 直接 panic；即使有，systemd 也不知道挂哪 | 注入 `/pianoinit` shim：翻译 `piano.root=`→`root=`、`ro`→`rw`，再 `exec /init` |

另外 `init_boot`/`vendor_boot` 都是 header v4，解析时要用 v3+ 的头部布局
（`kernel_size@8, ramdisk_size@12, os_version@16, header_size@20, header_version@40, cmdline@44`），
不能套 v0–v2 的 `kernel_addr/ramdisk_addr/page_size` 布局。

## 3. 注入的两个东西

### 3.1 `/pianoinit`（cmdline 翻译）

固件 cmdline 不可改，所以只能改「systemd 看到的 cmdline」：把改写后的内容 bind mount 到
`/proc/cmdline`，再 `exec /init`。PID 1 不变，上游 `piano-physical-platform` 与 systemd
流程原样保留。

```
piano.root=LABEL=PIANOROOT  →  root=LABEL=PIANOROOT
ro                          →  rw
```

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

1. **备份 ESP**：`sudo dd if=/dev/sda35 of=/home/piano/esp-backup.img bs=1M`；
2. 复制四件套到 `/boot/efi/EFI/Piano/stable/`（覆盖前把原文件另存一份）；
3. 重启进 fastboot，刷 `sunuefi_root`（上游 `root.sparse.img`，未压缩约 60 GiB 逻辑大小，
   本机分区 99.5 GiB，容量足够）；
4. 重启。

**顺序不能颠倒**：先换 ESP 再刷 root。反过来的话，中间那次重启是「旧内核 + 新 rootfs」
（内核版本对不上，起不来），而「新内核 + 旧 rootfs」还可能进系统但硬件全丢。

## 6. 回退

- ESP 有备份：fastboot 刷回 `esp-backup.img`，或从 `/boot/efi` 里另存的原文件拷回。
- root：本机原系统会被覆盖，**这是不可逆的**，除非事先做过 root 分区镜像。
  只想试内核的话，不要刷 root，改 cmdline 里的 `piano.root=` 指向别的分区即可
  （cmdline 在固件里，做不到，所以实际不可行——这也是为什么这套方案属于「整机替换」）。

## 7. 与 port-stage-esp.py 的关系

| 契约 | 使用者 | 产物 |
| --- | --- | --- |
| `SUNUEFI-LINUXv2` payload | 另一版加载器（`LinuxRamBoot.c` 读 `\EFI\Piano\stable\boot.img`，magic 校验 144B 头） | `Image.efi` / `piano.dtb` / `initramfs.cpio.gz` / `config` |
| Android boot v2 四件套 | 本机当前固件 | `Image` / `board.dtb` / `initramfs` / `boot.img` |

两者**不能混用**。刷之前先用 `hexdump -C boot.img | head -1` 确认本机固件是哪一套：
`ANDROID!` 走本文件；`SUNUEFI-LINUXv2` 走 `port-stage-esp.py`。
