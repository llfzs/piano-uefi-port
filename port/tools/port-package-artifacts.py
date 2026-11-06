#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：三产物打包（需求 4）。

把上游构建产物 + SunUEFI 引导助手组装成与 Project-SunUEFI
.github/workflows/build-products.yml 完全一致的三个 GitHub Actions artifact：

  SunUEFI-Module/          SunUEFI-Piano-<version>.zip   引导助手模块
  SunUEFI-ESP/             esp.sparse.img                启动镜像（各发行版共用）
  SunUEFI-Root-<target>/   root-<target>.sparse.img     系统镜像

Android sparse 编码与 SunUEFI tools/package_sparse_images.py 逐字段对齐：
  header  <IHHHHIIII = MAGIC 0xed26ff3a, ver=1, 0, 28, 12, block, blocks, chunks, 0
  chunk   <HHII      = kind(RAW 0xcac1 / FILL 0xcac2), 0, count, 12+payload
  零块用 FILL，保证回读完全一致；RAW 每块 < 64MiB 以兼容 fastboot 传输上限。

本脚本只写普通文件，不碰设备、不执行 fastboot。离线可跑、可自检。
输入（均可选，缺省则生成带 status=PLACEHOLDER 的空 artifact 骨架 + 说明，
便于「同步上游后先把 CI 骨架跑绿」）：
  --module-zip   SunUEFI 引导助手模块 ZIP
  --esp-image    FAT32 ESP 的 raw 镜像（或已编码的 .sparse.img）
  --root-image   ext4 root 的 raw 镜像（或已编码的 .sparse.img）
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

MAGIC = 0xed26ff3a
RAW, FILL = 0xCAC1, 0xCAC2
BLOCK = 4096
SPARSE_HEADER = struct.Struct("<IHHHHIIII")  # 28 bytes
CHUNK_HEADER = struct.Struct("<HHII")        # 12 bytes


def sha256(path: Path) -> str:
    with path.open("rb") as s:
        return hashlib.file_digest(s, "sha256").hexdigest()


def is_sparse(path: Path) -> bool:
    with path.open("rb") as s:
        head = s.read(4)
    return head == struct.pack("<I", MAGIC)


def to_sparse(source: Path, destination: Path) -> dict:
    """SunUEFI package_sparse_images.py::convert 的等价实现。"""
    size = source.stat().st_size
    if not size or size % BLOCK or size // BLOCK > 0xFFFFFFFF:
        raise SystemExit(f"raw 镜像必须是非空且 4K 对齐: {source} ({size}B)")
    chunks = blocks = 0
    expanded = hashlib.sha256()
    zero = bytes(BLOCK)
    with source.open("rb") as src, destination.open("xb+") as dst:
        dst.write(bytes(SPARSE_HEADER.size))
        kind = None
        start = count = 0

        def finish():
            nonlocal chunks
            if not count:
                return
            end = dst.tell()
            payload = count * BLOCK if kind == RAW else 4
            dst.seek(start)
            dst.write(CHUNK_HEADER.pack(kind, 0, count, CHUNK_HEADER.size + payload))
            dst.seek(end)
            chunks += 1

        while batch := src.read(4 * 1024 * 1024):
            expanded.update(batch)
            for off in range(0, len(batch), BLOCK):
                data = batch[off:off + BLOCK]
                next_kind = FILL if data == zero else RAW
                if next_kind != kind or (kind == RAW and count * BLOCK >= 64 * 1024 * 1024):
                    finish()
                    kind, count, start = next_kind, 0, dst.tell()
                    dst.write(bytes(CHUNK_HEADER.size))
                    if kind == FILL:
                        dst.write(bytes(4))
                if kind == RAW:
                    dst.write(data)
                count += 1
                blocks += 1
        finish()
        dst.seek(0)
        dst.write(SPARSE_HEADER.pack(MAGIC, 1, 0, SPARSE_HEADER.size,
                                     CHUNK_HEADER.size, BLOCK, blocks, chunks, 0))
    return {
        "bytes": destination.stat().st_size, "expanded_bytes": size,
        "sha256": sha256(destination), "expanded_sha256": expanded.hexdigest(),
        "format": "android-sparse", "block_bytes": BLOCK,
        "total_blocks": blocks, "chunks": chunks,
    }


def place_image(src: Path, dst: Path) -> dict:
    """已 sparse 的直接复制保真，raw 的现场编码。返回镜像记录。"""
    if is_sparse(src):
        data = src.read_bytes()
        dst.write_bytes(data)
        row = {"bytes": len(data), "format": "android-sparse (passthrough)",
               "sha256": sha256(dst), "source": src.name}
        # 读回 header 里的 blocks/chunks 供 manifest
        try:
            vals = SPARSE_HEADER.unpack_from(data, 0)
            row["total_blocks"], row["chunks"] = vals[6], vals[7]
            row["block_bytes"] = vals[5]
        except struct.error:
            pass
        return row
    return to_sparse(src, dst)


def sums_for(files: dict[str, Path]) -> str:
    return "".join(sha256(p) + "  " + name + "\n" for name, p in sorted(files.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--distros", type=Path, required=True, help="distros.json")
    ap.add_argument("--output", type=Path, required=True, help="artifact 根目录")
    ap.add_argument("--target", default="", help="只打某个 target；缺省打所有 enabled")
    ap.add_argument("--module-zip", type=Path, default=None)
    ap.add_argument("--esp-image", type=Path, default=None)
    ap.add_argument("--root-image", type=Path, default=None)
    ap.add_argument("--version", default="0.0.0-port", help="Module ZIP 版本后缀")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    profile = json.loads(a.profile.read_text())
    distros = json.loads(a.distros.read_text())
    selected = [d for d in distros["distros"] if d["enabled"] and
                (not a.target or d["target"] == a.target)]
    if not selected:
        raise SystemExit("没有启用的 target（检查 distros.json / --target）")

    out = a.output.resolve()
    if out.exists() and any(out.iterdir()) and not a.force:
        raise SystemExit(f"输出目录非空，需 --force: {out}")
    out.mkdir(parents=True, exist_ok=True)

    artifacts: dict = {
        "schema_version": 1,
        "status": "ARTIFACTS_PACKAGED_HOST_ONLY",
        "profile_schema": profile["schema"],
        "partition_esp": profile["partitions"]["esp"]["name"],
        "partition_root": profile["partitions"]["root"]["name"],
        "root_policy": profile["partitions"]["root_policy"],
        "artifacts": {},
    }

    # ---- 1. SunUEFI-Module（共享）----
    module_dir = out / "SunUEFI-Module"
    module_dir.mkdir(parents=True, exist_ok=True)
    module_files: dict[str, Path] = {}
    if a.module_zip and a.module_zip.is_file():
        target_zip = module_dir / f"SunUEFI-Piano-{a.version}.zip"
        target_zip.write_bytes(a.module_zip.read_bytes())
        module_files[target_zip.name] = target_zip
        module_status = "READY"
    else:
        readme = module_dir / "README.txt"
        readme.write_text(
            "SunUEFI-Module 占位。\n"
            "该产物是 Android 引导助手模块 ZIP（SunUEFI-Piano-<version>.zip），\n"
            "由 Project-SunUEFI 的 tools/build_android_product_module.py 构建（需 ARM64 交叉编译）。\n"
            "CI 中传入 --module-zip <该 ZIP> 即会替换本占位为真实模块。\n"
            "它负责创建 sunuefi_esp/sunuefi_root、刷写两个 sparse 镜像、写 BootRequest 切换。\n")
        module_files[readme.name] = readme
        module_status = "PLACEHOLDER_NEEDS_SUNUEFI_MODULE_BUILD"
    (module_dir / "SHA256SUMS").write_text(sums_for(module_files))
    artifacts["artifacts"]["SunUEFI-Module"] = {
        "status": module_status, "dir": module_dir.name,
        "files": {n: {"bytes": p.stat().st_size, "sha256": sha256(p)} for n, p in module_files.items()},
    }

    # ---- 2. SunUEFI-ESP（共享）----
    esp_dir = out / "SunUEFI-ESP"
    esp_dir.mkdir(parents=True, exist_ok=True)
    esp_files: dict[str, Path] = {}
    if a.esp_image and a.esp_image.is_file():
        esp_img = esp_dir / "esp.sparse.img"
        esp_row = place_image(a.esp_image, esp_img)
        esp_files[esp_img.name] = esp_img
        esp_status = "READY"
    else:
        readme = esp_dir / "README.txt"
        readme.write_text(
            "SunUEFI-ESP 占位。\n"
            "真实产物 esp.sparse.img 来自 port/tools/port-stage-esp.py 生成的 ESP 目录，\n"
            "经 mkfs.vfat 打成 FAT32 raw 镜像后，由本脚本编码为 Android sparse。\n"
            "CI 传入 --esp-image <esp.raw|.sparse.img> 即生成真实镜像。\n"
            "刷写目标分区：sunuefi_esp（绝不写 vbmeta/dtbo/_b/userdata）。\n")
        esp_files[readme.name] = readme
        esp_row, esp_status = None, "PLACEHOLDER_NEEDS_ESP_BUILD"
    (esp_dir / "SHA256SUMS").write_text(sums_for(esp_files))
    esp_entry = {"status": esp_status, "dir": esp_dir.name,
                 "partition": profile["partitions"]["esp"]["name"],
                 "files": {n: {"bytes": p.stat().st_size, "sha256": sha256(p)} for n, p in esp_files.items()}}
    if esp_row:
        esp_entry["image"] = esp_row
    artifacts["artifacts"]["SunUEFI-ESP"] = esp_entry

    # ---- 3. SunUEFI-Root-<target>（每发行版）----
    root_status = "PLACEHOLDER_NEEDS_ROOTFS_BUILD"
    for d in selected:
        root_dir = out / d["artifact"]
        root_dir.mkdir(parents=True, exist_ok=True)
        root_files: dict[str, Path] = {}
        root_status = "PLACEHOLDER_NEEDS_ROOTFS_BUILD"
        root_row = None
        if a.root_image and a.root_image.is_file():
            root_img = root_dir / d["root_image"]
            root_row = place_image(a.root_image, root_img)
            root_files[root_img.name] = root_img
            root_status = "READY"
        else:
            readme = root_dir / "README.txt"
            readme.write_text(
                f"{d['artifact']} 占位。\n"
                f"真实产物 {d['root_image']} 来自 {d['source']}，\n"
                "经 port-inject-login.py（piano/piano + root piano）与 "
                "port-adapt-rootfs.py（fstab/root-policy）后处理后，\n"
                "由本脚本编码为 Android sparse。CI 传入 --root-image 即生成真实镜像。\n"
                "刷写目标分区：sunuefi_root（标签 PIANOROOT，ext4）。\n")
            root_files[readme.name] = readme
        (root_dir / "SHA256SUMS").write_text(sums_for(root_files))
        entry = {"status": root_status, "dir": root_dir.name,
                 "target": d["target"], "source": d["source"],
                 "partition": profile["partitions"]["root"]["name"],
                 "root_label": profile["partitions"]["root_label"],
                 "login": {"user": profile["login"]["user"],
                           "password": profile["login"]["password"],
                           "root_password": profile["login"]["root_password"]},
                 "files": {n: {"bytes": p.stat().st_size, "sha256": sha256(p)} for n, p in root_files.items()}}
        if root_row:
            entry["image"] = root_row
        artifacts["artifacts"][d["artifact"]] = entry

    (out / "manifest.json").write_text(json.dumps(artifacts, indent=2) + "\n")
    print(json.dumps(artifacts, indent=2))
    statuses = (module_status, esp_status, root_status)
    ph = [s for s in statuses if s.startswith("PLACEHOLDER")]
    if ph:
        print("\n[OK] 三产物骨架已生成（仅文件，未刷写任何设备）。", file=sys.stderr)
        print(f"[warn] {len(ph)} 项仍为占位骨架；发布门禁将强制草稿 + 预发布。",
              file=sys.stderr)
    else:
        print("\n[OK] 三产物真实镜像已生成（仅文件，未刷写任何设备）。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
