#!/usr/bin/env python3
"""把 port 层的 /etc 契约合并进 Project-SunUEFI 的 Android 模块基座。

背景
----
port 仓库自己拼的 `SunUEFI-Piano-<tag>.zip` 只有约 8 KB 配置文本，里面没有
运行时：没有 `payload/app.bin`（UEFI 应用本体）、没有 `bin/piano-boot-repack`
与 `bin/piano-boot-request`（改 BOOT / 写 BootRequest）、没有 `bin/e2fsck` +
`bin/resize2fs`（扩容）、没有 `piano-storage` 与 WebUI。装上去既不能建分区也
不能切系统，`port-switch.sh` 一跑就 command not found。

真正的运行时由 Project-SunUEFI 构建，发布为 `SunUEFI-Module.tar.zst`
（tar 里是 `SunUEFI-Piano-<ver>.zip`，约 21 MB 压缩 / 36 MB 解压）。

本工具做的是「基座 + 增量」，不重造基座：
  * 原样保留基座的 bin/、payload/*.bin、webroot/、customize.sh、manager.sh、
    action.sh、storage*.sh、native-interface.json、policy.json、licenses/、sources/
  * 追加 `payload/etc/`（fstab / sudoers / udev / gdm3 / piano 策略）
  * 追加 `payload/account/{passwd,group,shadow}`（账号，单独放，按用户名合并）
  * 追加 `piano-port-config.sh`，由它接管首次启动的账号注入
  * 重写 `service.sh`：**先**跑 port 注入，**再**跑原厂扩容校验
    （原厂 service.sh 是 `[ -f ... ] || exit 0` 短路，直接往后追加永远跑不到）
  * `module.prop` 沿用基座 id，同名覆盖安装，避免两个模块并存抢 BOOT

为什么账号要按用户名合并而不是整体覆盖
--------------------------------------
旧的 `cp -a payload/etc/. /mnt/etc/` 会把 rootfs 的 /etc/passwd（Debian 40+
个系统账号）覆盖成 port 生成的那一两行，uid 映射随之错乱，systemd / dbus /
polkit 全部起不来。这里改成逐行按用户名合并：同名替换、缺则追加，其余保留。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

# 需要按用户名合并的账号文件（放在 payload/account/）
ACCOUNT_FILES = ("passwd", "group", "shadow")
# 原样覆盖的配置（放在 payload/etc/）
ETC_FILES = (
    "fstab",
    "gdm3/custom.conf",
    "piano/root-policy.json",
    "sudoers.d/10-piano-port",
    "udev/rules.d/01-piano-protect-android.rules",
)

PORT_CONFIG_SH = r'''#!/system/bin/sh
# SunUEFI port: 把账号与挂载契约写进 sunuefi_root。首次启动执行一次，幂等。
#
# 与旧版 module 的差异：不再整体覆盖 /etc/passwd|group|shadow，而是按用户名
# 合并——rootfs 自带的 root/daemon/nobody 等系统账号必须保留，覆盖会让 uid
# 映射错乱（systemd、dbus、polkit 都会起不来）。同名行替换、缺失行追加。
MODDIR=${MODDIR:-${0%/*}}
ROOTDEV=${PIANO_ROOT_DEV:-/dev/block/by-name/sunuefi_root}
MNT=${PIANO_ROOT_MNT:-/data/local/tmp/piano-root-mnt}
FLAG=/etc/piano/.sunuefi-firstboot-done

[ -e "$ROOTDEV" ] || exit 0
mkdir -p "$MNT" 2>/dev/null || exit 0
mount -t ext4 -o rw "$ROOTDEV" "$MNT" 2>/dev/null || exit 0

if [ -f "$MNT$FLAG" ]; then
    umount "$MNT" 2>/dev/null
    rmdir "$MNT" 2>/dev/null
    exit 0
fi

mkdir -p "$MNT/etc/piano" "$MNT/home/piano"

# 1) 账号：逐行按用户名合并
for f in passwd group shadow; do
    src="$MODDIR/payload/account/$f"
    [ -f "$src" ] || continue
    dst="$MNT/etc/$f"
    [ -f "$dst" ] || : > "$dst"
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        key=$(printf '%s' "$line" | cut -d: -f1)
        [ -n "$key" ] || continue
        if grep -q "^$key:" "$dst" 2>/dev/null; then
            awk -v k="$key" -v v="$line" '{
                n = index($0, ":")
                if (n > 0 && substr($0, 1, n - 1) == k) print v; else print
            }' "$dst" > "$dst.port" && mv -f "$dst.port" "$dst"
        else
            printf '%s\n' "$line" >> "$dst"
        fi
    done < "$src"
done
chmod 0644 "$MNT/etc/passwd" 2>/dev/null
chmod 0644 "$MNT/etc/group" 2>/dev/null
chmod 0640 "$MNT/etc/shadow" 2>/dev/null

# 2) 其余配置：整体覆盖（这些本来就是 port 层要改的挂载与策略契约）
for rel in fstab gdm3/custom.conf sudoers.d/10-piano-port \
           udev/rules.d/01-piano-protect-android.rules piano/root-policy.json; do
    src="$MODDIR/payload/etc/$rel"
    [ -f "$src" ] || continue
    case "$rel" in
        */*) mkdir -p "$MNT/etc/${rel%/*}" 2>/dev/null ;;
    esac
    cp -f "$src" "$MNT/etc/$rel" 2>/dev/null || true
done
chmod 0440 "$MNT/etc/sudoers.d/10-piano-port" 2>/dev/null

chown 1000:1000 "$MNT/home/piano" 2>/dev/null
touch "$MNT$FLAG" 2>/dev/null
umount "$MNT" 2>/dev/null
rmdir "$MNT" 2>/dev/null
exit 0
'''

SERVICE_SH = r'''#!/system/bin/sh
# 由 piano-uefi-port 重写：保留 Project-SunUEFI 原逻辑，前面挂 port 账号注入。
#
# 原厂 service.sh 是 `[ -f .../android-expansion.json ] || exit 0` 短路，
# 往后直接追加 port 逻辑永远执行不到，所以这里改成 if 分支。
MODPATH=${MODDIR:-${0%/*}}

# port 层：首次启动把 piano 账号与挂载契约写进 sunuefi_root（幂等，失败不阻塞）
/system/bin/sh "$MODPATH/piano-port-config.sh" >/dev/null 2>&1 || true

# 原厂：Android 扩容后的一致性校验（只读，执行一次）
if [ -f /data/adb/piano-sunuefi/storage/android-expansion.json ]; then
    /system/bin/sh "$MODPATH/storage-operations.sh" verify-expansion >/dev/null 2>&1
fi
exit 0
'''


# ------------------------------------------------------------------ 工具


def log(msg: str) -> None:
    print(f"[module-merge] {msg}", file=sys.stderr, flush=True)


def _zstd_decompress(src: Path, dst: Path) -> None:
    if shutil.which("zstd"):
        with dst.open("wb") as fo:
            r = subprocess.run(["zstd", "-d", "-q", "-c", str(src)], stdout=fo)
        if r.returncode != 0:
            raise SystemExit(f"zstd 解压失败({r.returncode}): {src}")
        return
    try:
        import zstandard  # type: ignore
    except ImportError:
        raise SystemExit("需要 zstd：apt-get install -y zstd 或 pip install zstandard")
    dctx = zstandard.ZstdDecompressor()
    with src.open("rb") as fi, dst.open("wb") as fo:
        dctx.copy_stream(fi, fo)


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    root = dest.resolve()
    for m in tar.getmembers():
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(root)):
            raise SystemExit(f"tar 成员越界: {m.name}")
    tar.extractall(dest)


def load_base(base: Path, stage: Path) -> dict:
    """把基座解成模块目录（stage），返回元信息。

    支持三种输入：
      * SunUEFI-Module.tar.zst —— Project-SunUEFI release 的原样产物，
        tar 里是一个 `SunUEFI-Piano-<ver>.zip`
      * SunUEFI-Piano-<ver>.zip —— 直接给模块 zip
      * 目录 —— 已解开的模块目录（离线自检用）
    """
    meta = {"base": str(base), "base_form": "", "base_module_zip": "",
            "version": "", "version_code": 0, "id": ""}
    if base.is_dir():
        meta["base_form"] = "dir"
        shutil.copytree(base, stage, dirs_exist_ok=True)
    elif base.suffix == ".zst" or base.name.endswith(".tar.zst"):
        meta["base_form"] = "tar.zst"
        with tempfile.TemporaryDirectory() as td:
            tarpath = Path(td) / "module.tar"
            _zstd_decompress(base, tarpath)
            inner = Path(td) / "inner"
            inner.mkdir()
            with tarfile.open(tarpath) as tf:
                _safe_extract_tar(tf, inner)
            zips = sorted(inner.glob("SunUEFI-Piano-*.zip")) or sorted(inner.glob("*.zip"))
            if not zips:
                raise SystemExit(f"基座 tar 里没有模块 zip: {base}")
            if len(zips) > 1:
                log(f"!! 基座里有 {len(zips)} 个 zip，取 {zips[0].name}")
            meta["base_module_zip"] = zips[0].name
            with zipfile.ZipFile(zips[0]) as z:
                z.extractall(stage)
    elif base.suffix == ".zip":
        meta["base_form"] = "zip"
        meta["base_module_zip"] = base.name
        with zipfile.ZipFile(base) as z:
            z.extractall(stage)
    else:
        raise SystemExit(f"不认识的基座格式: {base}")

    prop = stage / "module.prop"
    if not prop.is_file():
        raise SystemExit(f"基座缺 module.prop，不是 Android 模块: {base}")
    for line in prop.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k == "id":
            meta["id"] = v
        elif k == "version":
            meta["version"] = v
        elif k == "versionCode":
            try:
                meta["version_code"] = int(v)
            except ValueError:
                pass
    if not (stage / "payload").is_dir():
        raise SystemExit(f"基座缺 payload/，不是引导助手模块: {base}")
    # 关键运行时检查：缺了这些就退化成 8 KB 空壳
    required = ("bin/piano-boot-repack", "bin/piano-boot-request", "payload/app.bin")
    missing = [r for r in required if not (stage / r).is_file()]
    if missing:
        raise SystemExit(
            "基座不是完整的 SunUEFI-Module（缺 %s）。请用 Project-SunUEFI 的 "
            "SunUEFI-Module.tar.zst，而不是 port 自己拼的配置包。" % ", ".join(missing))
    return meta


def stage_payload(payload: Path, stage: Path) -> dict:
    """把 port 生成的 /etc 拆成「账号」与「其余配置」两类放进去。"""
    if not payload.is_dir():
        raise SystemExit(f"缺 port payload 目录: {payload}")
    account_dir = stage / "payload/account"
    etc_dir = stage / "payload/etc"
    account_dir.mkdir(parents=True, exist_ok=True)
    etc_dir.mkdir(parents=True, exist_ok=True)

    accounts, etcs, modes = [], [], {}
    for p in sorted(payload.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(payload).as_posix()
        if rel in ACCOUNT_FILES:
            shutil.copyfile(p, account_dir / rel)
            mode = 0o640 if rel == "shadow" else 0o644
            os.chmod(account_dir / rel, mode)
            modes[f"payload/account/{rel}"] = mode
            accounts.append(rel)
        else:
            dst = etc_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dst)
            mode = 0o440 if rel.startswith("sudoers.d/") else 0o644
            os.chmod(dst, mode)
            modes[f"payload/etc/{rel}"] = mode
            etcs.append(rel)
    # 账号文件必须有 passwd/group/shadow 三件套才谈得上登录
    missing = [a for a in ACCOUNT_FILES if a not in accounts]
    if missing:
        log(f"!! payload 缺账号文件 {missing}（登录很可能不可用）")
    return {"account": accounts, "etc": etcs, "modes": modes}


def patch_scripts(stage: Path) -> dict:
    """写 port 注入脚本并重写 service.sh。"""
    cfg = stage / "piano-port-config.sh"
    cfg.write_text(PORT_CONFIG_SH, encoding="utf-8", newline="\n")
    os.chmod(cfg, 0o755)

    svc = stage / "service.sh"
    original = svc.read_text(encoding="utf-8", errors="replace") if svc.is_file() else ""
    svc.write_text(SERVICE_SH, encoding="utf-8", newline="\n")
    os.chmod(svc, 0o755)
    return {"service_sh_rewritten": bool(original), "port_config_sh": "piano-port-config.sh",
            "modes": {"piano-port-config.sh": 0o755, "service.sh": 0o755}}


def patch_prop(stage: Path, tag: str, meta: dict) -> dict:
    """module.prop 沿用基座 id（同名覆盖），version 加 port 后缀。"""
    prop = stage / "module.prop"
    suffix = f"port.{tag}" if tag else "port"
    out: list[str] = []
    seen: set[str] = set()
    new_version = ""
    for line in prop.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            out.append(line)
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k == "version":
            v = f"{v}+{suffix}"
            new_version = v
        elif k == "versionCode":
            # 比基座大 1，保证能覆盖安装已存在的同 id 模块
            v = str(int(meta.get("version_code") or 0) + 1)
        elif k == "description":
            v = (v + " [piano-uefi-port: 首启注入 piano 账号与 sunuefi_root 挂载契约]").strip()
        out.append(f"{k}={v}")
        seen.add(k)
    if "version" not in seen:
        new_version = f"{meta.get('version', '')}+{suffix}"
        out.append(f"version={new_version}")
    if "versionCode" not in seen:
        out.append(f"versionCode={int(meta.get('version_code') or 0) + 1}")
    prop.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    os.chmod(prop, 0o644)
    return {"id": meta.get("id", ""), "version": new_version,
            "version_code": int(meta.get("version_code") or 0) + 1}


def repackage(stage: Path, out: Path, src_modes: dict[str, int],
              extra_modes: dict[str, int] | None = None) -> dict:
    """打成模块 zip，沿用基座里各文件的权限位。

    Magisk / KernelSU 直接解 zip，所以 `bin/*` 的 0755 必须保住；否则装完执行
    权限全丢，customize.sh 的 set_perm 之外就没救了。

    新增文件不能用 `stat()` 取模式：Windows 上 chmod 基本无效，本地自检会拿到
    0o666 写进 zip。所以新增文件的模式由调用方显式给出（extra_modes）。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    extra = extra_modes or {}
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(stage).as_posix()
            zi = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            mode = extra.get(rel)
            if mode is None:
                mode = src_modes.get(rel)
            if mode is None:
                mode = 0o755 if (rel.endswith(".sh") or rel.startswith("bin/")) else 0o644
            zi.external_attr = (stat.S_IFREG | mode) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            with p.open("rb") as fi:
                z.writestr(zi, fi.read())
    return {"path": str(out), "bytes": out.stat().st_size,
            "sha256": _sha256(out), "files": len(files)}


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_base_modes(base: Path) -> dict[str, int]:
    """记录基座 zip 里各文件的权限位（tar.zst 形式要先解出内层 zip）。"""
    modes: dict[str, int] = {}
    zpath: Path | None = None
    if base.suffix == ".zip":
        zpath = base
    elif base.name.endswith(".tar.zst") or base.suffix == ".zst":
        with tempfile.TemporaryDirectory() as td:
            tarpath = Path(td) / "m.tar"
            _zstd_decompress(base, tarpath)
            with tarfile.open(tarpath) as tf:
                for m in tf.getmembers():
                    if m.name.endswith(".zip"):
                        tarpath2 = Path(td) / "inner.zip"
                        with tarpath2.open("wb") as fo:
                            fo.write(tf.extractfile(m).read())  # type: ignore[arg-type]
                        zpath = tarpath2
                        break
            if zpath is not None:
                with zipfile.ZipFile(zpath) as z:
                    for zi in z.infolist():
                        modes[zi.filename] = (zi.external_attr >> 16) & 0o777
                return modes
    if zpath is None:
        return modes
    with zipfile.ZipFile(zpath) as z:
        for zi in z.infolist():
            modes[zi.filename] = (zi.external_attr >> 16) & 0o777
    return modes


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="把 port 层契约合并进 SunUEFI-Module 基座")
    ap.add_argument("--base", required=True, type=Path,
                    help="SunUEFI-Module.tar.zst / SunUEFI-Piano-*.zip / 已解开的模块目录")
    ap.add_argument("--payload", required=True, type=Path,
                    help="port 层生成的 rootfs /etc 目录（port-inject-login + port-adapt-rootfs 的产物）")
    ap.add_argument("--out", required=True, type=Path, help="输出模块 zip")
    ap.add_argument("--tag", default="", help="上游/构建 tag，写进 version 后缀")
    ap.add_argument("--json", action="store_true", help="把结果以 JSON 打到 stdout")
    a = ap.parse_args()

    if not a.base.exists():
        raise SystemExit(f"基座不存在: {a.base}")
    modes = collect_base_modes(a.base)
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "module"
        stage.mkdir()
        meta = load_base(a.base, stage)
        log(f"基座: {meta['base_form']} id={meta['id']} version={meta['version']} "
            f"code={meta['version_code']}")
        staged = stage_payload(a.payload, stage)
        log(f"payload: 账号 {staged['account']} 配置 {len(staged['etc'])} 个")
        scripts = patch_scripts(stage)
        prop = patch_prop(stage, a.tag, meta)
        modes_all = dict(modes)
        modes_all.update(staged["modes"])
        modes_all.update(scripts["modes"])
        modes_all["module.prop"] = 0o644
        info = repackage(stage, a.out, modes_all)

    result = {
        "status": "READY_WITH_RUNTIME",
        "base": meta,
        "payload": staged,
        "scripts": scripts,
        "module_prop": prop,
        "output": info,
    }
    log(f"产物: {info['path']}  {info['bytes'] / 2**20:.1f} MiB  {info['files']} 文件")
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
