#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：把源仓库(xiaomipad-8pro-mainline)的内核四件套重新布局成 SunUEFI ESP。

输入全部是源仓库已有的构建产物，不重新编译内核：
  --image    源仓库 build-kernel.sh -> arch/arm64/boot/Image (未压缩 ARM64)
  --dtb      源仓库 prepare-boot-dtb.py -> piano-boot.dtb (完整合并 FDT)
  --initrd   源仓库 build-initramfs.sh -> initramfs.cpio.gz
  --profile  port/config/port-profile.json

输出（--output 目录下，随后整体 rsync 到 sunuefi_esp 分区）:
  EFI/Piano/stable/Image.efi
  EFI/Piano/stable/piano.dtb
  EFI/Piano/stable/initramfs.cpio.gz
  EFI/Piano/stable/config          (cmdline)
  EFI/Piano/stable/boot.img        (SUNUEFI-LINUXv2 payload, 给 RAM loader / 合体 BOOT)
  EFI/Piano/shared/dtb-manifest.json
  port-esp-manifest.json           (全量 sha256 记录, 供 verify 使用)

只写普通文件，绝不碰磁盘/分区/刷机。
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path


def sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def check_arm64_image(data: bytes) -> None:
    """宽松校验 ARM64 Image: linux_banner 或 ARM64 魔数。真机 EFI Stub 另由 config 保证。"""
    if len(data) < 64 * 1024:
        raise SystemExit("Image 过小，不像未压缩内核")
    if b"Linux version" in data[:4096]:
        return
    # arm64 Image header: code0 = 0x14000000 (b insn) 附近；这里只做非空 sanity
    if data[:4] == b"\x00" * 4 and data.count(b"\x00") == len(data):
        raise SystemExit("Image 全零，疑似占位文件")


def check_fdt(data: bytes) -> None:
    """完整合并 FDT 校验，与 SunUEFI package_kernel_payload.py 一致。"""
    if len(data) < 40 or data[:4] != b"\xd0\x0d\xfe\xed":
        raise SystemExit("DTB 缺少 FDT 魔数 0xd00dfeed，或过小")
    totalsize = struct.unpack_from(">I", data, 4)[0]
    if totalsize != len(data):
        raise SystemExit(f"DTB totalsize({totalsize}) != 实际长度({len(data)})")


def build_payload(kernel: bytes, initrd: bytes, dtb: bytes) -> bytes:
    """SUNUEFI-LINUXv2 header, 与 SunUEFI package_kernel_payload.py 完全对齐。

    struct: <16s I I Q Q 32s 32s Q 32s = 144 bytes
    magic, version=2, header_size=144, kernel_len, initrd_len,
    kernel_sha256, initrd_sha256, dtb_len, dtb_sha256
    """
    header = struct.pack(
        "<16sIIQQ32s32sQ32s",
        b"SUNUEFI-LINUXv2\0", 2, 144,
        len(kernel), len(initrd),
        sha(kernel), sha(initrd),
        len(dtb), sha(dtb),
    )
    assert len(header) == 144, "header 长度必须与 SunUEFI 契约一致"
    return header + kernel + initrd + dtb


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--dtb", type=Path, required=True)
    ap.add_argument("--initrd", type=Path, required=True)
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--force", action="store_true", help="允许输出目录已存在")
    a = ap.parse_args()

    for p in (a.image, a.dtb, a.initrd, a.profile):
        if not p.is_file():
            raise SystemExit(f"缺少输入文件: {p}")

    profile = json.loads(a.profile.read_text())
    root_uuid = profile["partitions"]["root"]["partuuid"]
    esp_uuid = profile["partitions"]["esp"]["partuuid"]

    kernel = a.image.read_bytes()
    dtb = a.dtb.read_bytes()
    initrd = a.initrd.read_bytes()
    check_arm64_image(kernel)
    check_fdt(dtb)
    if not initrd:
        raise SystemExit("initramfs 为空")

    cmdline = profile["cmdline_template"].format(root_partuuid=root_uuid)

    out = a.output.resolve()
    if out.exists() and any(out.iterdir()) and not a.force:
        raise SystemExit(f"输出目录非空，需 --force: {out}")
    stable = out / "EFI" / "Piano" / "stable"
    shared = out / "EFI" / "Piano" / "shared"
    stable.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)

    (stable / "Image.efi").write_bytes(kernel)
    (stable / "piano.dtb").write_bytes(dtb)
    (stable / "initramfs.cpio.gz").write_bytes(initrd)
    (stable / "config").write_text(cmdline + "\n")
    payload = build_payload(kernel, initrd, dtb)
    (stable / "boot.img").write_bytes(payload)

    dtb_manifest = {
        "source": "xiaomipad-8pro-mainline / tools/prepare-boot-dtb.py -> piano-boot.dtb",
        "dtb_sha256": sha(dtb).hex(),
        "bytes": len(dtb),
        "board_topology_verified": False,
        "note": "合并自 mainline-dtb + stock-base + stock-overlay（源仓库已有逻辑）",
    }
    (shared / "dtb-manifest.json").write_text(json.dumps(dtb_manifest, indent=2) + "\n")

    manifest = {
        "status": "HOST_STAGED_NOT_FLASHED",
        "profile_schema": profile["schema"],
        "esp_partuuid": esp_uuid,
        "root_partuuid": root_uuid,
        "cmdline": cmdline,
        "kernel": {"file": "EFI/Piano/stable/Image.efi", "bytes": len(kernel), "sha256": sha(kernel).hex()},
        "dtb": {"file": "EFI/Piano/stable/piano.dtb", "bytes": len(dtb), "sha256": sha(dtb).hex()},
        "initramfs": {"file": "EFI/Piano/stable/initramfs.cpio.gz", "bytes": len(initrd), "sha256": sha(initrd).hex()},
        "payload": {"file": "EFI/Piano/stable/boot.img", "magic": "SUNUEFI-LINUXv2", "header_bytes": 144,
                    "bytes": len(payload), "sha256": sha(payload).hex()},
        "contract_gap": profile["contract_gap"]["id"],
    }
    (out / "port-esp-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(json.dumps(manifest, indent=2))
    print("\n[OK] ESP 布局已生成（仅文件，未刷写任何设备）。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
