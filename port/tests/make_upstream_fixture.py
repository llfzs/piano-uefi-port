#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""生成「上游 Release」离线 fixture，用于验证 upstream-release -> UEFI 三产物链路。

不联网、不下载真实 3.5GB 资产；按上游 v1.0.0 的 assets-manifest.json 结构，
合成等价的小型 boot.zip / rootfs 分卷 / tools.zip / SHA256SUMS.assets，
从而在沙箱里完整跑通 port-from-upstream-release.sh 与后续打包/发布门禁。

用途：
  python3 tests/make_upstream_fixture.py <输出目录>
  bash port/build/port-from-upstream-release.sh --fork <tree> --inputs <dir> \
       --fixture <输出目录>
"""
import gzip
import hashlib
import json
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

TAG = "v1.0.0-fixture"
PREFIX = f"piano-ubuntu-{TAG}"
MAGIC_SPARSE = 0xED26FF3A
RAW = 0xCAC1


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def boot_image(kernel: bytes, ramdisk: bytes) -> bytes:
    """Android Boot Image header v2（上游 build-boot.py 用 --header_version 2）。"""
    page = 4096
    header_size = 1660
    hdr = bytearray(header_size)
    hdr[0:8] = b"ANDROID!"
    struct.pack_into("<I", hdr, 8, len(kernel))       # kernel_size
    struct.pack_into("<I", hdr, 12, 0x8000)           # kernel_addr
    struct.pack_into("<I", hdr, 16, len(ramdisk))     # ramdisk_size
    struct.pack_into("<I", hdr, 20, 0x1000000)        # ramdisk_addr
    struct.pack_into("<I", hdr, 24, 0)                # second_size
    struct.pack_into("<I", hdr, 36, page)             # page_size
    struct.pack_into("<I", hdr, 40, 2)                # header_version = 2
    struct.pack_into("<I", hdr, 1644, header_size)    # header_size
    struct.pack_into("<I", hdr, 1648, 0)              # dtb_size
    cmd = b"root=PARTLABEL=linux_rootfs rootfstype=ext4 rw rootwait console=ttyMSM0"
    hdr[64:64 + len(cmd)] = cmd

    def align(n):
        return (n + page - 1) // page * page

    out = bytearray(align(header_size))
    out[:len(hdr)] = hdr
    out += kernel
    out += b"\x00" * (align(len(out)) - len(out))
    out += ramdisk
    out += b"\x00" * (align(len(out)) - len(out))
    return bytes(out)


def fake_kernel() -> bytes:
    # 裸 ARM64 Image：>=64KiB，前 4KiB 含 linux_banner，供 port-stage-esp.py 校验
    k = bytearray(128 * 1024)
    banner = b"Linux version 7.2.14-piano56 (fixture) SMP PREEMPT aarch64\x00"
    k[:len(banner)] = banner
    for i in range(len(banner), len(k), 4096):
        k[i:i + 8] = struct.pack("<Q", 0x14000000 + (i // 4096))
    return bytes(k)


def fake_dtb() -> bytes:
    # 最小合法 FDT：magic + totalsize 与实际长度一致
    d = bytearray(256)
    d[0:4] = b"\xd0\x0d\xfe\xed"
    struct.pack_into(">I", d, 4, len(d))
    struct.pack_into(">I", d, 8, 40)   # off_dt_struct
    struct.pack_into(">I", d, 12, 48)  # off_dt_strings
    struct.pack_into(">I", d, 16, 40)  # off_mem_rsvmap
    struct.pack_into(">I", d, 20, 17)  # version
    struct.pack_into(">I", d, 36, 8)   # size_dt_struct
    d[40:48] = struct.pack(">II", 1, 0)  # FDT_BEGIN_NODE + root
    d[48:52] = struct.pack(">I", 2)      # FDT_END_NODE
    return bytes(d)


def fake_sparse(blocks: int = 256) -> bytes:
    """小型 Android sparse（全部 RAW），供 root.sparse.img 透传验证。"""
    blk = 4096
    header = struct.Struct("<IHHHHIIII")
    chunk = struct.Struct("<HHII")
    hdr = header.pack(MAGIC_SPARSE, 1, 0, header.size, chunk.size,
                      blk, blocks, 1, 0)
    ch = chunk.pack(RAW, 0, blocks, chunk.size + blocks * blk)
    payload = bytes((i % 251 for i in range(blocks * blk)))
    return hdr + ch + payload


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("用法: make_upstream_fixture.py <输出目录>")
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)

    kernel = fake_kernel()
    ramdisk = gzip.compress(b"070701" + b"fixture initramfs" * 512, mtime=0)
    dtb = fake_dtb()
    sparse = fake_sparse()

    # ---- boot.zip：按上游 assets-manifest.json 的 contents 布局
    boot_zip = out / f"{PREFIX}-boot.zip"
    with zipfile.ZipFile(boot_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("RELEASE-NOTES.md", f"fixture {TAG}\n")
        z.writestr("boot/boot.img", boot_image(kernel, ramdisk))
        z.writestr("boot/init_boot.img", boot_image(kernel, ramdisk))
        z.writestr("boot/vendor_boot.img", boot_image(kernel, ramdisk))
        z.writestr("boot/boot-ram-check.img", boot_image(kernel, ramdisk))
        z.writestr("boot/piano-boot.dtb", dtb)
        z.writestr("boot/manifest.json", json.dumps({
            "kernel_commit": "fixture", "kernel_release": "7.2.14-piano56",
            "header_version": 2, "status": "OFFLINE_CANDIDATE_NOT_DEVICE_TESTED"}))
        z.writestr("rootfs/manifest.json", json.dumps({"root": "fixture"}))
        z.writestr("release-manifest.json", json.dumps({"release": TAG}))

    # ---- tools.zip
    tools_zip = out / f"{PREFIX}-tools.zip"
    with zipfile.ZipFile(tools_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("tools/piano-switch.py", "# fixture switch helper\n")
        z.writestr("tools/piano-switch-ksu-v0.2.0.zip", "fixture module\n")

    # ---- rootfs 分卷（zstd 压缩的 sparse，单卷即可验证 cat | zstd -d 路径）
    zst = subprocess.run(["zstd", "-T0", "-10", "--stdout"],
                         input=sparse, capture_output=True, check=True).stdout
    part0 = out / f"{PREFIX}-rootfs.img.zst.part0"
    part0.write_bytes(zst)

    # ---- metadata
    (out / "TAG").write_text(TAG + "\n")
    (out / "assets-manifest.json").write_text(json.dumps({
        "schema": 1, "release_name": TAG, "part_bytes": len(zst),
        "compression": ["zstd", "-T0", "-10", "--long=27"],
        "assets": [{"name": boot_zip.name, "bytes": boot_zip.stat().st_size},
                   {"name": tools_zip.name, "bytes": tools_zip.stat().st_size},
                   {"name": part0.name, "bytes": len(zst)}],
    }, indent=2) + "\n")
    (out / "upstream-release.json").write_text(json.dumps({
        "schema": 1, "source": "upstream-release",
        "repo": "yzddmr6/xiaomipad-8pro-mainline", "tag": TAG,
        "published_at": "2026-11-06T00:00:00Z", "target_commitish": "fixture-commit",
        "html_url": "", "name": TAG,
        "assets": [{"name": n.name, "asset_id": 0, "size": n.stat().st_size,
                    "role": "fixture", "api_url": ""}
                   for n in (boot_zip, tools_zip, part0)],
        "note": "离线 fixture（不联网），按上游 Release 资产结构合成",
    }, indent=2, ensure_ascii=False) + "\n")

    # ---- SHA256SUMS.assets（标准两空格格式）
    sums = []
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name not in ("SHA256SUMS.assets", "TAG"):
            sums.append(f"{sha(p.read_bytes())}  {p.name}\n")
    (out / "SHA256SUMS.assets").write_text("".join(sums))

    print(json.dumps({
        "fixture_dir": str(out), "tag": TAG,
        "files": {p.name: p.stat().st_size for p in sorted(out.iterdir()) if p.is_file()},
        "kernel_bytes": len(kernel), "dtb_bytes": len(dtb),
        "sparse_bytes": len(sparse), "zst_bytes": len(zst),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
