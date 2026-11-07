#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""上游 Release 适配器：把 yzddmr6/xiaomipad-8pro-mainline 的构建成品转成 UEFI 输入。

设计前提（需求 3「用上游源码、一直跟着更新」）：
  * 本仓库是独立 port 层，不含上游源码副本；
  * 上游 **不在 CI 里编译**（仓库无 workflow），镜像由维护者本地构建后按 tag 发 Release；
  * 因此「用上游源码构建」在 GitHub Actions 上的唯一可行形态，就是消费上游最新
    Release 里那些「由上游源码编译出来的成品」，而不是在 runner 上重编内核/rootfs
    （后者需要 QEMU chroot、Ubuntu ISO、原厂 DTB 与签名密钥，公共 runner 无法提供）。

本脚本只做两件事，全部离线可测：
  resolve  —— 查 GitHub API 拿到最新 Release 的资产清单，输出 downloads.tsv 与
              upstream-release.json（tag / commit / 每个资产的 sha256）。不写死 tag。
  adapt    —— 解 boot.zip 里的 A 槽三元组，抽出裸 Image / initramfs / 合并 DTB，
              交给 port-native-esp.py 生成本机固件读取的四件套 ESP
              （Image / board.dtb / initramfs / boot.img，boot.img 为 Android boot v2）；
              生成含 piano/piano 账号与 sunuefi_root 挂载契约的 Module ZIP；输出
              port-inputs.env。上游 root.sparse.img 是 Android sparse，原样透传给打包器。

不联网之外的副作用、不改上游文件、不碰设备、不执行 fastboot。
"""
import argparse
import gzip
import hashlib
import json
import os
import shlex
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com"
DEFAULT_REPO = "yzddmr6/xiaomipad-8pro-mainline"
MAGIC_BOOT = b"ANDROID!"


def log(*a):
    print("[upstream-release]", *a, file=sys.stderr)


def gh_get(path: str, token: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sunuefi-port-upstream-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(API + path, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
    raise SystemExit(f"GitHub API 请求失败: {path}: {last}")


def role_of(name: str) -> str:
    if name == "SHA256SUMS.assets":
        return "checksums"
    if name == "assets-manifest.json":
        return "manifest"
    if name.endswith("-boot.zip"):
        return "boot"
    if name.endswith("-tools.zip"):
        return "tools"
    if "-rootfs.img.zst.part" in name:
        return "root-part"
    return "skip"  # packages.zip 等：UEFI 三产物不需要


# ------------------------------------------------------------------ resolve
def cmd_resolve(a) -> int:
    repo = a.repo
    path = f"/repos/{repo}/releases/"
    tag = a.tag.strip()
    if tag:
        release = gh_get(path + "tags/" + tag, a.token)
    else:
        release = gh_get(path + "latest", a.token)
    got_tag = release.get("tag_name")
    if not got_tag:
        raise SystemExit("上游 Release 无 tag_name（可能是草稿）")

    out = Path(a.inputs).resolve()
    out.mkdir(parents=True, exist_ok=True)
    assets = []
    for it in release.get("assets", []):
        name = it["name"]
        assets.append({
            "name": name,
            "asset_id": it["id"],
            "size": it["size"],
            "role": role_of(name),
            "api_url": f"{API}/repos/{repo}/releases/assets/{it['id']}",
        })
    if not any(x["role"] == "boot" for x in assets):
        raise SystemExit(f"上游 {got_tag} 未发现 *-boot.zip，资产结构可能变了")
    if not any(x["role"] == "checksums" for x in assets):
        raise SystemExit(f"上游 {got_tag} 缺 SHA256SUMS.assets，拒绝跳过校验")

    (out / "downloads.tsv").write_text(
        "".join(f"{x['name']}\t{x['asset_id']}\t{x['size']}\t{x['role']}\t{x['api_url']}\n"
                for x in assets), encoding="utf-8")
    meta = {
        "schema": 1,
        "source": "upstream-release",
        "repo": repo,
        "tag": got_tag,
        "published_at": release.get("published_at"),
        "target_commitish": release.get("target_commitish"),
        "html_url": release.get("html_url"),
        "name": release.get("name"),
        "assets": assets,
        "note": "由上游源码构建、维护者本地编译后发布的成品；本仓库只做 UEFI 化后处理。",
    }
    (out / "upstream-release.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"tag": got_tag, "assets": len(assets),
                      "boot": [x["name"] for x in assets if x["role"] == "boot"],
                      "root_parts": [x["name"] for x in assets if x["role"] == "root-part"]},
                     indent=2))
    log(f"上游最新 Release = {got_tag}（未写死 tag，上游发新版自动跟随）")
    return 0


# ------------------------------------------------------- android boot image
def align(n: int, page: int) -> int:
    return (n + page - 1) // page * page


def unpack_vendor_boot(data: bytes) -> dict:
    """解析 vendor_boot v3/v4，返回 vendor_ramdisk。

    布局（v3/v4 一致的部分）：`[header][vendor_ramdisk][dtb]`，v4 只是在这之后**追加**
    `vendor_ramdisk_table` 与 `bootconfig`，ramdisk 的起始位置仍是页对齐后的 header 末尾。

    实测上游 v1.0.0：header_size=2128、page=4096、vendor_ramdisk_size=51673969，
    ramdisk 从 4096 开始、以 zstd magic 开头（不是 gzip）。

    为什么必须专门解析：上游 boot.img 的 ramdisk 为 0，init_boot.img 的 ramdisk 只有
    48 B（gzip 空包）。真实 ramdisk 只在 vendor_boot 里，而 v0..v2 的 boot 解析顺序
    （kernel/ramdisk/second/dtb 依次页对齐）对 VNDRBOOT 完全不适用；按 grep 找 gzip
    magic 也找不到——它是 zstd。
    """
    if data[:8] != b"VNDRBOOT":
        raise ValueError("不是 Android vendor_boot（缺 VNDRBOOT magic）")
    u32 = lambda o: struct.unpack_from("<I", data, o)[0]
    hv = u32(8)
    page = u32(12)
    if page not in (2048, 4096, 8192, 16384, 32768, 65536, 131072):
        page = 4096
    ramdisk_size = u32(24)
    header_size = u32(2096) if len(data) >= 2100 and 0 < u32(2096) < 16384 else 2112
    off = align(header_size, page)
    if ramdisk_size <= 0 or off + ramdisk_size > len(data):
        raise ValueError(f"vendor_boot ramdisk 段越界（off={off} size={ramdisk_size} "
                         f"file={len(data)}）")
    return {"header_version": hv, "page": page,
            "ramdisk": data[off:off + ramdisk_size]}


def unpack_boot_image(data: bytes) -> dict:
    """解析 Android Boot Image header v0..v4，返回 kernel / ramdisk / dtb 原始段。"""
    if len(data) < 64 or data[:8] != MAGIC_BOOT:
        raise ValueError("不是 Android Boot Image（缺 ANDROID! magic）")
    u32 = lambda o: struct.unpack_from("<I", data, o)[0]
    hv = u32(40)
    if hv >= 3:
        kernel_size, ramdisk_size = u32(8), u32(12)
        header_size = u32(20)
        page = 4096
        off = align(header_size, page)
        kernel = data[off:off + kernel_size]
        off = align(off + kernel_size, page)
        ramdisk = data[off:off + ramdisk_size]
        return {"header_version": hv, "kernel": kernel, "ramdisk": ramdisk,
                "dtb": b"", "page": page}

    kernel_size, ramdisk_size, second_size = u32(8), u32(16), u32(24)
    page = u32(36)
    if page not in (2048, 4096, 8192, 16384, 32768, 65536, 131072):
        page = 4096
    header_size = u32(1644) if hv >= 1 and len(data) >= 1648 and 0 < u32(1644) < 8192 else 1632
    dtb_size = u32(1648) if hv >= 2 and len(data) >= 1652 else 0
    off = align(header_size, page)
    kernel = data[off:off + kernel_size]
    off = align(off + kernel_size, page)
    ramdisk = data[off:off + ramdisk_size]
    off = align(off + ramdisk_size, page)
    off = align(off + second_size, page)
    dtb = data[off:off + dtb_size] if dtb_size else b""
    return {"header_version": hv, "kernel": kernel, "ramdisk": ramdisk,
            "dtb": dtb, "page": page}


def decompress_kernel(blob: bytes) -> tuple[bytes, str]:
    if blob[:2] == b"\x1f\x8b":
        return gzip.decompress(blob), "gzip"
    if blob[:4] in (b"\x02\x21\x4c\x18", b"\x04\x22\x4d\x18"):
        try:
            r = subprocess.run(["lz4", "-d", "-"], input=blob,
                               capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout:
                return r.stdout, "lz4"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        raise ValueError("内核是 lz4 压缩，但环境缺 lz4 工具（CI 需 apt install liblz4-tool）")
    return blob, "raw"


def find_fdt(data: bytes) -> bytes:
    """boot.zip 缺独立 DTB 时，从 vendor_boot 里找 FDT 魔数兜底。"""
    pos = data.find(b"\xd0\x0d\xfe\xed")
    if pos < 0 or pos + 8 > len(data):
        return b""
    total = struct.unpack_from(">I", data, pos + 4)[0]
    if 40 <= total <= len(data) - pos:
        return data[pos:pos + total]
    return b""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- module zip
MODULE_SERVICE = r'''#!/system/bin/sh
# SunUEFI port / piano firstboot injection.
# Runs on the Android side (KernelSU/Magisk module service.sh).
# The Linux root image is the unmodified upstream root.sparse.img (60 GiB ext4).
# We mount sunuefi_root once and inject the account + mount contract that the
# upstream image does not carry (upstream has no user account and uses
# PARTLABEL=linux_rootfs / linux_data).
MODDIR=${MODDIR:-${0%/*}}
ROOTDEV=/dev/block/by-name/sunuefi_root
[ -e "$ROOTDEV" ] || exit 0
MNT=/data/local/tmp/piano-root-mnt
FLAG=/etc/piano/.sunuefi-firstboot-done
mkdir -p "$MNT" 2>/dev/null || exit 0
mount -t ext4 -o rw "$ROOTDEV" "$MNT" 2>/dev/null || exit 0
if [ ! -f "$MNT$FLAG" ]; then
    mkdir -p "$MNT/etc/piano" "$MNT/home/piano"
    cp -a "$MODDIR/payload/etc/." "$MNT/etc/" 2>/dev/null
    chown 1000:1000 "$MNT/home/piano" 2>/dev/null
    touch "$MNT$FLAG" 2>/dev/null
fi
umount "$MNT" 2>/dev/null
rmdir "$MNT" 2>/dev/null
'''

MODULE_PROP = """id=sunuefi_piano
name=SunUEFI Piano
version={version}
versionCode=1
author=Project-SunUEFI port
description=Create sunuefi_esp/sunuefi_root, inject piano/piano login and write BootRequest
"""


def build_module_zip(fork: Path, inputs: Path, tag: str, out: Path) -> dict:
    """用已有的 inject-login / adapt-rootfs 产出真实配置文件，打进 Module ZIP。"""
    import shutil
    import tempfile

    profile = fork / "port/config/port-profile.json"
    if not profile.is_file():
        raise SystemExit(f"缺 port-profile.json: {profile}")

    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        rootfs = stage / "rootfs"
        (rootfs / "etc").mkdir(parents=True)
        (rootfs / "bin").mkdir()
        (rootfs / "bin/.keep").write_text("")
        # 复用已校验的桥接脚本生成账号 + 挂载契约
        subprocess.run([sys.executable, str(fork / "port/tools/port-inject-login.py"),
                        "--rootfs", str(rootfs), "--profile", str(profile)],
                       check=True, capture_output=True, text=True)
        subprocess.run([sys.executable, str(fork / "port/tools/port-adapt-rootfs.py"),
                        "--rootfs", str(rootfs), "--profile", str(profile)],
                       check=True, capture_output=True, text=True)

        mod = stage / "module"
        payload_etc = mod / "payload/etc"
        shutil.copytree(rootfs / "etc", payload_etc, dirs_exist_ok=True)
        (mod / "module.prop").write_text(MODULE_PROP.format(version=tag or "0.0.0-port"))
        (mod / "service.sh").write_text(MODULE_SERVICE)
        (mod / "service.sh").chmod(0o755)
        # 分区计划 + 切换脚本：Module 创建分区 / 写 BootRequest 的依据
        plan_out = stage / "plan"
        subprocess.run([sys.executable, str(fork / "port/tools/port-partition-plan.py"),
                        "--profile", str(profile), "--output", str(plan_out)],
                       check=True, capture_output=True, text=True)
        for p in plan_out.rglob("*"):
            if p.is_file():
                shutil.copy2(p, mod / p.name)
        sw = fork / "port/tools/port-switch.sh"
        if sw.is_file():
            shutil.copy2(sw, mod / "port-switch.sh")
        (mod / "upstream-release.txt").write_text(
            f"upstream tag: {tag}\nroot image: upstream root.sparse.img (passthrough)\n"
            "account + fstab + root-policy injected by service.sh on first boot\n")

        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for p in sorted(mod.rglob("*")):
                if p.is_file():
                    zi = zipfile.ZipInfo(p.relative_to(mod).as_posix(),
                                         date_time=(1980, 1, 1, 0, 0, 0))
                    zi.external_attr = (0o755 if p.suffix == ".sh" else 0o644) << 16
                    z.writestr(zi, p.read_bytes())
    return {"path": str(out), "bytes": out.stat().st_size, "sha256": _file_sha(out)}


def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as s:
        for b in iter(lambda: s.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------- adapt
def cmd_adapt(a) -> int:
    fork = Path(a.fork).resolve()
    inputs = Path(a.inputs).resolve()
    profile = fork / "port/config/port-profile.json"
    if not profile.is_file():
        raise SystemExit(f"缺 port-profile.json: {profile}")

    meta = {}
    mpath = inputs / "upstream-release.json"
    if mpath.is_file():
        meta = json.loads(mpath.read_text())
    tag = meta.get("tag", a.tag or "unknown")

    # 1) 定位并解 boot.zip
    boots = sorted(inputs.glob("*-boot.zip"))
    if not boots:
        raise SystemExit("缺上游 *-boot.zip（下载/校验步骤未成功）")
    bootzip = boots[0]
    with zipfile.ZipFile(bootzip) as z:
        infos = z.infolist()
        names = [i.filename for i in infos]
        def member(suffix):
            hit = [n for n in names if n.endswith(suffix)]
            return z.read(hit[0]) if hit else b""
        dtb = member("boot/piano-boot.dtb")
        boot_img = member("boot/boot.img")
        init_boot = member("boot/init_boot.img")
        vendor_boot = member("boot/vendor_boot.img")
        ram_check = member("boot/boot-ram-check.img")
    log(f"boot.zip = {bootzip.name}，{len(names)} 个成员")
    for i in sorted(infos, key=lambda x: x.filename):
        if i.filename.startswith("boot/") and i.filename.endswith(".img"):
            log(f"  {i.filename}: {i.file_size}B")

    # 2) 抽内核：优先正式 boot.img，缺则 boot-ram-check.img（同一套 kernel/initrd/DTB）
    candidates = [("boot.img", boot_img), ("boot-ram-check.img", ram_check)]
    parsed = None
    used = ""
    for label, blob in candidates:
        if not blob:
            continue
        try:
            parsed = unpack_boot_image(blob)
            used = label
            break
        except ValueError as e:
            log(f"  {label} 不可解析: {e}")
    if parsed is None:
        raise SystemExit("无法从 boot.zip 解析出 Android Boot Image（上游格式已变，需同步适配）")

    kernel, kfmt = decompress_kernel(parsed["kernel"])
    if len(kernel) < 64 * 1024:
        raise SystemExit(f"抽出的内核过小({len(kernel)}B, 格式{kfmt})，疑似不是裸 ARM64 Image")
    (inputs / "Image").write_bytes(kernel)

    # ramdisk 选取顺序：boot.img → init_boot.img → vendor_boot.img，取「第一个像样的」。
    # 上游 v1.0.0 实测：boot.img 的 ramdisk 为 0；init_boot.img 的 ramdisk 只有 48 B
    # （gzip 空包）；真 ramdisk 在 vendor_boot.img（zstd，约 49 MiB）。
    # 旧逻辑把 48 B 当成有效值直接采用，于是 CI 产物里的 initramfs 是个空包——
    # 能刷进去但内核一定 panic，属于「静默产出不可用固件」，这里必须显式判空。
    ramdisk = parsed["ramdisk"]
    ramdisk_source = used
    if len(ramdisk) < 4096 and init_boot:
        try:
            r = unpack_boot_image(init_boot)["ramdisk"]
            if len(r) > len(ramdisk):
                ramdisk, ramdisk_source = r, "init_boot.img"
        except ValueError as e:
            log(f"  init_boot.img 不可解析: {e}")
    if len(ramdisk) < 4096 and vendor_boot:
        try:
            r = unpack_vendor_boot(vendor_boot)["ramdisk"]
            if len(r) > len(ramdisk):
                ramdisk, ramdisk_source = r, "vendor_boot.img"
        except ValueError as e:
            log(f"  vendor_boot.img 不可解析: {e}")
    if len(ramdisk) < 4096:
        raise SystemExit(
            f"未找到可用 initramfs：最佳候选仅 {len(ramdisk)}B（boot/init_boot/vendor_boot "
            "都没给出真实 ramdisk）。上游 boot.zip 布局可能已变，拒绝产出起不来的固件。")
    log(f"  ramdisk 来源 {ramdisk_source}: {len(ramdisk)}B")
    (inputs / "initramfs.cpio.gz").write_bytes(ramdisk)

    if not dtb:
        dtb = parsed["dtb"] or find_fdt(vendor_boot)
    if not dtb or dtb[:4] != b"\xd0\x0d\xfe\xed":
        raise SystemExit("缺完整合并 DTB（boot/piano-boot.dtb），拒绝用内核原生 DTB 冒充")
    (inputs / "piano-boot.dtb").write_bytes(dtb)
    log(f"内核来源 {used}: Image {len(kernel)}B({kfmt}), initrd {len(ramdisk)}B, DTB {len(dtb)}B")

    # 3) 生成 ESP 目录：必须是本机固件真正读取的四件套
    #    （\EFI\Piano\stable\{Image,board.dtb,initramfs,boot.img}），
    #    boot.img 为 Android boot v2 容器；不能再用 port-stage-esp.py 的
    #    SUNUEFI-LINUXv2/Image.efi 那套命名——两套契约混用会直接开不了机。
    esp_dir = inputs / "esp-tree"
    if esp_dir.exists():
        import shutil
        shutil.rmtree(esp_dir)
    native = fork / "port/tools/native"
    for need in ("pianoinit.sh", "piano-root-fixup.sh"):
        if not (native / need).is_file():
            raise SystemExit(f"缺 native 注入脚本: port/tools/native/{need}")
    r = subprocess.run([sys.executable, str(fork / "port/tools/port-native-esp.py"),
                        "--kernel", str(inputs / "Image"),
                        "--dtb", str(inputs / "piano-boot.dtb"),
                        "--ramdisk", str(inputs / "initramfs.cpio.gz"),
                        "--init", str(native / "pianoinit.sh"),
                        "--fixup-script", str(native / "piano-root-fixup.sh"),
                        "--compress", "gzip",
                        "--output", str(esp_dir), "--force"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("port-native-esp.py 失败:\n" + (r.stderr or r.stdout))
    for msg in (r.stderr or "").strip().splitlines():
        log("  " + msg)

    # 4) Module ZIP（真实内容：账号 + fstab + root-policy + 分区计划 + 切换）
    module_zip = inputs / f"SunUEFI-Piano-{tag}.zip"
    minfo = build_module_zip(fork, inputs, tag, module_zip)

    # 5) root 镜像：上游 root.sparse.img 已是 Android sparse，原样透传
    root_candidates = [inputs / "root.sparse.img", inputs / "root.ext4", inputs / "root.image"]
    root_image = next((p for p in root_candidates if p.is_file() and p.stat().st_size > 0), None)
    root_status = "READY" if root_image else "MISSING_ROOT"

    env = {
        "DRY_RUN": "0",
        "INPUTS_DIR": str(inputs),
        "SOURCE": "upstream-release",
        "UPSTREAM_REPO": meta.get("repo", a.repo),
        "UPSTREAM_TAG": tag,
        "UPSTREAM_COMMIT": meta.get("target_commitish") or "",
        "UPSTREAM_HTML_URL": meta.get("html_url") or "",
        "FULL_BUILD_STATUS": "READY" if root_image else "PARTIAL_NO_ROOT",
        "ESP_DIR": str(esp_dir),
        "ESP_IMAGE": "",            # 由 shell 用 mkfs.vfat/mcopy 生成后追加
        "ROOT_IMAGE": str(root_image) if root_image else "",
        "MODULE_ZIP": str(module_zip),
        "DTB_STATUS": "UPSTREAM_MERGED_DTB",
        "KERNEL_SOURCE": used,
        "ROOT_STATUS": root_status,
    }
    # 必须用 shlex.quote：KERNEL_SOURCE 之类的值含空格（"boot.img + init_boot.img"），
    # 裸写会让 workflow 的 `source` 把 "+" 当命令执行（"+: command not found", 127）。
    (inputs / "port-inputs.env").write_text(
        "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()), encoding="utf-8")

    note = ("root 镜像为上游 root.sparse.img 原样透传；piano/piano 账号与 "
            "sunuefi_root 挂载契约由 Module ZIP 的 service.sh 首次启动时注入。"
            "真机引导需验证 G1 挂载 / G2 EFI Stub / G3 OS4.0.5 BOOT 头兼容。")

    report = {
        "status": env["FULL_BUILD_STATUS"],
        "source": "upstream-release",
        "upstream_tag": tag,
        "kernel_source": used,
        "kernel_format": kfmt,
        "kernel_bytes": len(kernel),
        "initramfs_bytes": len(ramdisk),
        "initramfs_source": ramdisk_source,
        "initramfs_suspicious": False,   # <4096B 已在上游直接 SystemExit，走到这里必为真 ramdisk
        "dtb_bytes": len(dtb),
        "esp_dir": str(esp_dir),
        "module": minfo,
        "root_image": str(root_image) if root_image else None,
        "root_status": root_status,
        "note": note,
    }
    (inputs / "upstream-adapt.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="解析上游最新 Release 资产清单")
    r.add_argument("--repo", default=DEFAULT_REPO)
    r.add_argument("--tag", default="", help="留空=自动取 latest（跟随上游更新）")
    r.add_argument("--inputs", required=True)
    r.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    r.set_defaults(func=cmd_resolve)

    d = sub.add_parser("adapt", help="把上游成品转成 UEFI 三产物输入")
    d.add_argument("--fork", required=True, help="本仓库根（含 port/）")
    d.add_argument("--inputs", required=True)
    d.add_argument("--repo", default=DEFAULT_REPO)
    d.add_argument("--tag", default="")
    d.set_defaults(func=cmd_adapt)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
