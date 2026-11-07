#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""把上游成品组装成 SunUEFI 固件**实际读取**的四文件 ESP 布局。

为什么不能复用 port-stage-esp.py
--------------------------------
本机固件（`\\EFI\\Piano\\stable\\`）读四个文件：Image / board.dtb / initramfs / boot.img，
其中 boot.img 是 **Android boot v2** 容器：kernel + ramdisk + 内嵌 DTB（与 board.dtb 同内容），
`header_size=1660`、`dtb_size`/`dtb_addr` 按原厂实测值写，cmdline 为空。
port-stage-esp.py 产出的是 SUNUEFI-LINUXv2 单文件 payload + Image.efi 命名，属于另一套
加载器契约；两者混用会直接开不了机。

为什么必须改 initramfs
----------------------
固件的 cmdline 编译在固件里，固定为 `rdinit=/pianoinit piano.root=LABEL=PIANOROOT ... ro`，
而上游 initramfs 是 systemd 驱动的，只认 `root=`。若不处理，内核找不到 /pianoinit 直接 panic。

本脚本注入两个东西（都不改上游文件内容）：
  1. `/pianoinit`     —— 把 `piano.root=` 翻译成 `root=`、把 `ro` 改成 `rw`，再 exec 上游 `/init`；
  2. fstab 修正单元   —— 上游 rootfs 的 fstab 指向它自己的 linux_rootfs/linux_data，
                         本机不存在这两块分区，首次启动时注释掉并补 /home。

只写普通文件，不碰设备、不刷机。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

PAGE = 4096
MAGIC = b"ANDROID!"
HEADER_VERSION = 2
# 以下四个地址/长度取自本机原厂 boot.img 的实测值，不是猜的：
#   header_size@1644 = 1660，recovery_dtbo_size@1632 = 0，
#   dtb_size@1648(u32) = 1212784，dtb_addr@1652(u64) = 0x11F00000，
#   且该 DTB 与 ESP 里的 board.dtb 逐字节相同（dtb 段跟在 ramdisk 之后、页对齐）。
# 早期版本把 header_size 留成 0、只写 1632，属于两处错位：1632 是 recovery_dtbo_size，
# v2 的 dtb_size 在 1648，而 header_size 缺失会让按头长度定位 kernel/ramdisk 的
# bootloader 解析错位。这里按原厂布局原样复刻。
KERNEL_ADDR = 0x10008000
RAMDISK_ADDR = 0x11000000
TAGS_ADDR = 0x10000100
DTB_ADDR = 0x11F00000
HEADER_SIZE = 1660


# --------------------------------------------------------------- compression
def _zstd_module():
    try:
        import zstandard  # noqa: F401
        return zstandard
    except ImportError:
        return None


def debomb(path: Path) -> bytes:
    blob = path.read_bytes()
    if blob[:2] == b"\x1f\x8b":
        return gzip.decompress(blob)
    if blob[:4] == b"\x28\xb5\x2f\xfd":
        mod = _zstd_module()
        if mod:
            with open(path, "rb") as fh:
                return mod.ZstdDecompressor().stream_reader(fh).read()
        if shutil.which("zstd"):
            return subprocess.run(["zstd", "-dc", str(path)], check=True,
                                  stdout=subprocess.PIPE).stdout
        raise SystemExit("ramdisk 是 zstd 压缩：装 python 包 zstandard，或装 zstd 可执行文件")
    if blob[:6] == b"\xfd7zXZ\x00":
        if not shutil.which("xz"):
            raise SystemExit("ramdisk 是 xz 压缩，需要 xz 可执行文件")
        return subprocess.run(["xz", "-dc", str(path)], check=True,
                              stdout=subprocess.PIPE).stdout
    if blob[:6] in (b"070701", b"070702"):
        return blob
    raise SystemExit(f"无法识别的 ramdisk 压缩格式: {blob[:4]!r}")


def compress(raw: bytes, mode: str) -> bytes:
    if mode == "gzip":
        return gzip.compress(raw, compresslevel=6, mtime=0)
    if mode == "zstd":
        mod = _zstd_module()
        if mod:
            return mod.ZstdCompressor(level=19).compress(raw)
        if shutil.which("zstd"):
            with tempfile.NamedTemporaryFile(suffix=".cpio") as src, \
                    tempfile.NamedTemporaryFile(suffix=".zst") as dst:
                src.write(raw)
                src.flush()
                subprocess.run(["zstd", "-q", "-f", "-19", str(src.name), "-o", dst.name], check=True)
                return Path(dst.name).read_bytes()
        raise SystemExit("要求 zstd 压缩：装 python 包 zstandard，或装 zstd 可执行文件")
    return raw


# ---------------------------------------------------------------- cpio newc
def cpio_entries(raw: bytes):
    off = 0
    while off + 110 <= len(raw):
        hdr = raw[off:off + 110]
        if hdr[:6] not in (b"070701", b"070702"):
            raise ValueError(f"不是 newc cpio（偏移 {off} magic={hdr[:6]!r}）")
        n = [int(hdr[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        (ino, mode, uid, gid, nlink, mtime, filesize,
         devmajor, devminor, rdevmajor, rdevminor, namesize, check) = n
        name = raw[off + 110:off + 110 + namesize - 1].decode("utf-8", "surrogateescape")
        # 文件名之后要按 4 字节对齐，数据才从这里开始（漏掉对齐会让每个文件整体偏移 1-3 字节）。
        pos = (off + 110 + namesize + 3) & ~3
        data = raw[pos:pos + filesize]
        yield dict(ino=ino, mode=mode, uid=uid, gid=gid, nlink=nlink, mtime=mtime,
                   devmajor=devmajor, devminor=devminor,
                   rdevmajor=rdevmajor, rdevminor=rdevminor, check=check,
                   name=name, data=data)
        off = (pos + filesize + 3) & ~3
        if name == "TRAILER!!!":
            # 内核支持多段拼接的 cpio（微码常见的作法就是前面挂一段独立归档）。
            # 遇到 TRAILER 不能直接收工，要跨过填充找下一段的魔数；否则后段文件
            # 在重新打包时会整体丢掉（实测两段 4 条目只解析出 2 个）。
            while off + 8 <= len(raw) and raw[off:off + 6] not in (b"070701", b"070702"):
                if raw[off] != 0:      # 非零字节说明后面不是归档，正常结束
                    return
                off += 1


def cpio_pack(entries) -> bytes:
    out = bytearray()
    for i, e in enumerate(entries):
        name = e["name"].encode("utf-8", "surrogateescape")
        data = e["data"]
        fields = (e.get("ino", i + 1), e["mode"], e.get("uid", 0), e.get("gid", 0),
                  e.get("nlink", 1), e.get("mtime", 0), len(data),
                  e.get("devmajor", 0), e.get("devminor", 0),
                  e.get("rdevmajor", 0), e.get("rdevminor", 0),
                  len(name) + 1, e.get("check", 0))
        out += b"070701" + b"".join(b"%08X" % v for v in fields)
        out += name + b"\0"
        out += b"\0" * (-len(out) % 4)
        out += data
        out += b"\0" * (-len(out) % 4)
    return bytes(out)


def ensure_parents(entries, path: str):
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        d = "/".join(parts[:i])
        if not any(e["name"].rstrip("/") == d for e in entries):
            entries.append(dict(name=d, mode=0o40755, data=b"", nlink=2))


def inject_file(entries, path, data: bytes, mode=0o100755):
    ensure_parents(entries, path)
    # 仓库 core.autocrlf 会把脚本检出成 CRLF；带 \r 的 shebang 在 initramfs 里跑不起来。
    data = data.replace(b"\r\n", b"\n")
    target = path.strip("/")
    for e in entries:
        if e["name"].rstrip("/") == target:
            e["data"] = data
            e["mode"] = mode
            return
    entries.append(dict(name=target, mode=mode, data=data, nlink=1))


def inject_symlink(entries, path, target: str):
    ensure_parents(entries, path)
    for e in entries:
        if e["name"].rstrip("/") == path.strip("/"):
            e["data"] = target.encode()
            e["mode"] = 0o120777
            return
    entries.append(dict(name=path.strip("/"), mode=0o120777,
                        data=target.encode(), nlink=1))


def trailer(entries):
    if entries and entries[-1]["name"] == "TRAILER!!!":
        return
    entries.append(dict(name="TRAILER!!!", mode=0, data=b"", nlink=1))


# ------------------------------------------------------------- android boot
def boot_id(kernel: bytes, ramdisk: bytes) -> bytes:
    h = hashlib.sha1()
    h.update(kernel)
    h.update(struct.pack("<I", len(kernel)))
    h.update(ramdisk)
    h.update(struct.pack("<I", len(ramdisk)))
    h.update(struct.pack("<I", 0))
    return h.digest() + b"\0" * 12


def build_boot_image(kernel: bytes, ramdisk: bytes, dtb: bytes = b"") -> bytes:
    hdr = bytearray(PAGE)
    hdr[0:8] = MAGIC
    struct.pack_into("<8I", hdr, 8, len(kernel), KERNEL_ADDR, len(ramdisk), RAMDISK_ADDR,
                     0, 0, TAGS_ADDR, PAGE)
    struct.pack_into("<I", hdr, 40, HEADER_VERSION)
    struct.pack_into("<I", hdr, 44, 0)
    hdr[48:64] = b"\0" * 16
    hdr[64:576] = b"\0" * 512          # 固件自己提供 cmdline，这里保持为空
    hdr[576:608] = boot_id(kernel, ramdisk)
    # v1 段：recovery_dtbo 不用；header_size 必须写真实长度（原厂 = 1660）。
    struct.pack_into("<I", hdr, 1632, 0)          # recovery_dtbo_size
    struct.pack_into("<Q", hdr, 1636, 0)          # recovery_dtbo_offset
    struct.pack_into("<I", hdr, 1644, HEADER_SIZE)
    # v2 段：dtb_size 是 u32@1648，dtb_addr 是 u64@1652（AOSP 结构体就是不对齐排的）。
    struct.pack_into("<I", hdr, 1648, len(dtb))
    if dtb:
        struct.pack_into("<Q", hdr, 1652, DTB_ADDR)
    out = bytearray(hdr)
    for blob in (kernel, ramdisk, dtb):
        if not blob:
            continue
        out += blob
        out += b"\0" * (-len(out) % PAGE)
    return bytes(out)


def parse_boot_image(data: bytes):
    if data[:8] != MAGIC:
        raise ValueError("缺 ANDROID! 魔数")
    ks, ka, rs, ra, ss, sa, ta, ps = struct.unpack_from("<8I", data, 8)
    hv = struct.unpack_from("<I", data, 40)[0]
    off = ps
    kernel = data[off:off + ks]
    off += (ks + ps - 1) // ps * ps
    ramdisk = data[off:off + rs]
    off += (rs + ps - 1) // ps * ps
    dtb_size = struct.unpack_from("<I", data, 1648)[0]
    dtb = data[off:off + dtb_size]
    return dict(kernel=kernel, ramdisk=ramdisk, header_version=hv, page_size=ps,
                kernel_addr=ka, ramdisk_addr=ra, header_size=struct.unpack_from("<I", data, 1644)[0],
                dtb=dtb, dtb_size=dtb_size,
                dtb_addr=struct.unpack_from("<Q", data, 1652)[0])


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kernel", type=Path, required=True, help="裸 ARM64 Image（须为 EFI Stub，MZ 头）")
    ap.add_argument("--dtb", type=Path, required=True, help="完整合并 FDT")
    ap.add_argument("--ramdisk", type=Path, required=True, help="上游 initramfs（zstd/gzip/xz/裸 cpio）")
    ap.add_argument("--init", type=Path, help="注入的 /pianoinit shim")
    ap.add_argument("--fixup-script", type=Path, help="注入的 rootfs 首次启动修正脚本")
    ap.add_argument("--compress", choices=("gzip", "zstd", "none"), default="gzip")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    kernel = a.kernel.read_bytes()
    dtb = a.dtb.read_bytes()
    if kernel[:2] != b"MZ":
        raise SystemExit("内核不是 EFI Stub PE 镜像（缺 MZ 头），固件无法加载")
    if dtb[:4] != b"\xd0\x0d\xfe\xed":
        raise SystemExit("DTB 缺 FDT 魔数 0xd00dfeed")
    if struct.unpack_from(">I", dtb, 4)[0] != len(dtb):
        raise SystemExit("DTB totalsize 与文件长度不一致（不是完整合并 FDT）")

    entries = list(cpio_entries(debomb(a.ramdisk)))
    # 每段归档都以 TRAILER!!! 收尾；注入项必须落在最后一个 TRAILER 之前，
    # 否则内核解包到第一个 TRAILER 就停，后面的文件等于没加（这个坑已经踩过一次）。
    # 多段拼接时中间也会有 TRAILER，所以全部去掉，最后统一补一个。
    segments = sum(1 for e in entries if e["name"] == "TRAILER!!!")
    entries = [e for e in entries if e["name"] != "TRAILER!!!"]
    if segments > 1:
        print(f"注意到 {segments} 段拼接 cpio，已合并为单段（顺序不变）", file=sys.stderr)
    orig_names = {e["name"] for e in entries}
    if "init" not in orig_names:
        raise SystemExit("上游 ramdisk 里没有 /init，无法在其之上做 shim（格式可能已变）")

    if a.init:
        inject_file(entries, "pianoinit", a.init.read_bytes())
        print(f"注入 /pianoinit ({a.init.stat().st_size}B)", file=sys.stderr)
    if a.fixup_script:
        inject_file(entries, "usr/libexec/piano/piano-root-fixup", a.fixup_script.read_bytes())
        unit = "\n".join([
            "[Unit]",
            "Description=SunUEFI rootfs contract fixup (fstab/home)",
            "DefaultDependencies=no",
            "After=sysroot.mount local-fs.target",
            "Before=initrd-switch-root.service",
            "",
            "[Service]",
            "Type=oneshot",
            "ExecStart=/usr/libexec/piano/piano-root-fixup",
            "",
            "[Install]",
            "WantedBy=initrd-root-fs.target",
            "",
        ])
        inject_file(entries, "usr/lib/systemd/system/piano-root-fixup.service", unit.encode(), 0o100644)
        inject_symlink(entries,
                       "usr/lib/systemd/system/initrd-root-fs.target.wants/piano-root-fixup.service",
                       "/usr/lib/systemd/system/piano-root-fixup.service")
        print("注入 rootfs 修正单元", file=sys.stderr)

    trailer(entries)
    raw_cpio = cpio_pack(entries)
    ramdisk = compress(raw_cpio, a.compress)

    out = a.output.resolve()
    if out.exists() and any(out.iterdir()) and not a.force:
        raise SystemExit(f"输出目录非空，需 --force: {out}")
    stable = out / "EFI" / "Piano" / "stable"
    stable.mkdir(parents=True, exist_ok=True)
    (stable / "Image").write_bytes(kernel)
    (stable / "board.dtb").write_bytes(dtb)
    (stable / "initramfs").write_bytes(ramdisk)
    boot = build_boot_image(kernel, ramdisk, dtb)
    (stable / "boot.img").write_bytes(boot)

    parsed = parse_boot_image(boot)
    assert parsed["kernel"] == kernel and parsed["ramdisk"] == ramdisk, "boot.img 回读不一致"
    assert parsed["header_size"] == HEADER_SIZE, "header_size 未落到 1660"
    assert parsed["dtb"] == dtb, "boot.img 内嵌 DTB 回读不一致"

    files = {}
    for name in ("Image", "board.dtb", "initramfs", "boot.img"):
        p = stable / name
        files[f"EFI/Piano/stable/{name}"] = {
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }

    manifest = {
        "schema": 1,
        "layout": "native-stable-quad",
        "status": "HOST_STAGED_NOT_FLASHED",
        "files": files,
        "boot_image": {"format": "android-boot-v2", "page_size": PAGE,
                       "kernel_addr": hex(KERNEL_ADDR), "ramdisk_addr": hex(RAMDISK_ADDR),
                       "dtb_in_boot": False, "cmdline_in_boot": False},
        "ramdisk": {"source": str(a.ramdisk), "entries_in": len(entries),
                    "compression": a.compress,
                    "pianoinit_injected": bool(a.init),
                    "fixup_injected": bool(a.fixup_script)},
    }
    (out / "native-esp-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(files, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
