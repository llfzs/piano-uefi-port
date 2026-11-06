#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：把源仓库的 Ubuntu 26.04 rootfs 适配到 SunUEFI 的 sunuefi_root 挂载契约。

源仓库 rootfs 假定挂在被重排的 linux_rootfs（60GiB）+ linux_data（/home）。
SunUEFI 方案不再重排 userdata，而是新建 sunuefi_esp + sunuefi_root 两块独立分区，
且要求 Android 原厂分区对 Linux 只读。本脚本在解出的 rootfs 目录里改写：
  etc/piano/root-policy.json
  etc/fstab                                   （改挂 PARTUUID=sunuefi_root）
  etc/udev/rules.d/01-piano-protect-android.rules（保护 Android 分区）
  etc/piano/mount-legacy-home.service         （可选：兼容旧的独立 /home 数据分区）

只写 rootfs 目录内的普通文件，不碰真实设备、不格式化、不刷机。
"""
import argparse
import json
import sys
from pathlib import Path

SAFE_SUBDIRS = ("etc", "etc/piano", "etc/udev", "etc/udev/rules.d",
                "etc/systemd", "etc/systemd/system", "boot", "boot/efi")


def refuse_symlinks(root: Path) -> None:
    for rel in SAFE_SUBDIRS:
        p = root / rel
        if p.is_symlink():
            raise SystemExit(f"拒绝写入穿透到宿主的符号链接目录: {rel}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rootfs", type=Path, required=True, help="已解出的 Ubuntu rootfs 目录")
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--legacy-home-uuid", default="",
                    help="可选：源仓库旧 linux_data 分区 PARTUUID，用于保留 /home 数据")
    a = ap.parse_args()

    root = a.rootfs.resolve()
    if not root.is_dir():
        raise SystemExit(f"rootfs 不是目录: {root}")
    if not (root / "etc").is_dir() or not (root / "bin").exists():
        raise SystemExit("看起来不是完整的 Linux rootfs（缺 etc/ 或 bin）")
    refuse_symlinks(root)

    profile = json.loads(a.profile.read_text())
    esp = profile["partitions"]["esp"]
    linux = profile["partitions"]["root"]
    rid, eid = linux["partuuid"], esp["partuuid"]

    for rel in SAFE_SUBDIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)

    # 1. SunUEFI root policy
    policy = {
        "version": 1, "mode": "partition",
        "root_partuuid": rid, "root_label": linux["name"],
        "esp_partuuid": eid, "esp_label": esp["name"],
    }
    (root / "etc/piano/root-policy.json").write_text(json.dumps(policy, indent=2) + "\n")

    # 2. fstab：主根挂 PARTUUID，ESP 挂 /boot/efi，Android 分区一律不挂
    lines = [
        "# SunUEFI port: dedicated partitions; never mount Android.",
        f"PARTUUID={rid}  /          ext4  defaults,noatime  0 1",
        f"PARTUUID={eid}  /boot/efi  vfat  umask=0077,nofail,x-systemd.device-timeout=10s  0 2",
    ]
    if a.legacy_home_uuid:
        lines.append(f"PARTUUID={a.legacy_home_uuid}  /home  ext4  defaults,noatime,nofail  0 2")
    (root / "etc/fstab").write_text("\n".join(lines) + "\n")

    # 3. udev：把 UFS 上除两块自有分区外的所有分区设为只读，防止误伤 Android
    rule = f'''# SunUEFI port: protect Android partitions from Linux writes.
SUBSYSTEM!="block", GOTO="piano_protect_end"
KERNELS!="1d84000.*", GOTO="piano_protect_end"
ENV{{UDISKS_IGNORE}}="1"
ENV{{DEVTYPE}}!="partition", GOTO="piano_protect_end"
IMPORT{{builtin}}="blkid"
ENV{{ID_PART_ENTRY_UUID}}=="{rid}", GOTO="piano_protect_end"
ENV{{ID_PART_ENTRY_UUID}}=="{eid}", GOTO="piano_protect_end"
ATTR{{ro}}=="1", GOTO="piano_protect_end"
RUN+="/usr/sbin/blockdev --setro /dev/%k"
LABEL="piano_protect_end"
'''
    (root / "etc/udev/rules.d/01-piano-protect-android.rules").write_text(rule)

    report = {
        "status": "ROOTFS_ADAPTED_HOST_ONLY",
        "rootfs": str(root),
        "root_partuuid": rid,
        "esp_partuuid": eid,
        "legacy_home": a.legacy_home_uuid or None,
        "written": ["etc/piano/root-policy.json", "etc/fstab",
                    "etc/udev/rules.d/01-piano-protect-android.rules"],
        "note": "需人工确认 initramfs init 与 root-policy.json 的挂载契约一致（见 G1）",
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
