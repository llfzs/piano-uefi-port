#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：生成 SunUEFI 分区方案的"操作计划"，只输出计划，绝不执行刷机。

源仓库方案：把 userdata 重排成 userdata + linux_rootfs(60G) + linux_data。
SunUEFI 方案：不动 userdata，新建 sunuefi_esp(512M vfat) + sunuefi_root(ext4)，
  由 Android 侧引导助手在 UFS 尾部腾出空间创建（见 SunUEFI install_piano.py）。

本脚本产出：
  partition-plan.json  —— 分区名/GUID/尺寸/文件系统/挂载点
  partition-commands.txt —— 供人工核对的命令清单（不执行）
"""
import argparse
import json
from pathlib import Path

ESP_GUID_TYPE = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"  # EFI System
LINUX_GUID_TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"  # Linux filesystem


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()

    profile = json.loads(a.profile.read_text())
    esp = profile["partitions"]["esp"]
    root = profile["partitions"]["root"]
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=True)

    plan = {
        "status": "PLAN_ONLY_NOT_APPLIED",
        "policy": "不重排 userdata；新建两块独立分区；Android 数据不清空",
        "new_partitions": [
            {"name": esp["name"], "label": esp["label"], "partition_guid": esp["partuuid"],
             "type_guid": ESP_GUID_TYPE, "filesystem": "vfat/FAT32",
             "capacity_bytes": esp["capacity_bytes"], "mount": "/boot/efi"},
            {"name": root["name"], "label": root["label"], "partition_guid": root["partuuid"],
             "type_guid": LINUX_GUID_TYPE, "filesystem": "ext4",
             "capacity_bytes": root["capacity_bytes"], "mount": "/"},
        ],
        "untouched": ["userdata", "boot_a", "boot_b", "init_boot_*", "vendor_boot_*",
                      "vbmeta*", "dtbo*", "persist", "modem", "firmware 相关分区"],
        "android_data_cleared": False,
    }
    (out / "partition-plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    cmds = [
        "# 仅供人工核对，脚本不会执行。真机创建分区请用 SunUEFI 引导助手（WebUI 存储页）",
        "# 或 SunUEFI tools/install_piano.py --execute（它自带容量与安全校验）。",
        "# 与源仓库 INSTALL.md 的关键差异：不再用 sgdisk 重排 userdata，不 format userdata。",
        "",
        "# ESP 与 root 由助手创建后，用 sparse 镜像写入：",
        "fastboot -s <SERIAL> -S 128M flash sunuefi_esp  esp.sparse.img",
        "fastboot -s <SERIAL> -S 128M flash sunuefi_root root.sparse.img",
        "",
        "# 严禁（沿用源仓库安全红线）：",
        "#   fastboot flash vbmeta* / dtbo* / *_b / persist / modem",
        "#   fastboot --set-active=b",
        "# 合体 BOOT 由 piano-boot-repack 无损打包，可用其 restore 还原原厂 BOOT。",
    ]
    (out / "partition-commands.txt").write_text("\n".join(cmds) + "\n")

    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
