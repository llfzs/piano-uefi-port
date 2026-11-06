#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：账号密码注入（需求 5）。

账号 piano / 密码 piano，root 密码 piano，GDM 自动登录。

为什么需要这一步：
  - 源仓库 tools/build-rootfs-image.py 的发布校验强约束 "无普通用户账号 +
    root 密码锁定"（no_user_accounts / root_password_locked），靠 Ubuntu OOBE
    首次开机创建用户；其 fstab 也是 PARTLABEL=linux_rootfs。
  - SunUEFI 方案（以及本需求）要预置 piano/piano 账号、root 可登录、自动登录。
  - 本脚本只生成配置供 Module ZIP 首次启动注入，**不改上游镜像本体、不碰设备**，
    而是在上游构建完成、拿到解出的 rootfs 目录树后做「后处理注入」。

对齐 SunUEFI：其 config/release.json login 段 + build_release_rootfs.py 用
  chroot <rootfs> chpasswd  < 'piano:piano123456'
设置登录密码；本脚本用离线写入 etc/shadow 的 sha512-crypt 哈希达到同一结果
（无需 root/chroot，可在 CI 与离线自检中运行），并把密码改为 piano，额外设置
root 密码与 GDM 自动登录。

用法：
  port-inject-login.py --rootfs <解出的 rootfs 目录> --profile port-profile.json
                       [--salt 固定盐，自检用] [--no-autologin]

只写 rootfs 目录内的普通文件，绝不碰真实设备。幂等：重复执行不会产生重复行。
"""
import argparse
import json
import secrets
import string
import sys
from pathlib import Path

# 只有这些系统账号文件会被改写，全部位于 rootfs 目录内。
PROTECTED_TARGETS = (
    "etc/passwd", "etc/shadow", "etc/group", "etc/gshadow",
    "etc/gdm3/custom.conf", "etc/gdm/custom.conf",
    "etc/sudoers.d/10-piano-port",
)


def make_sha512_crypt(password: str, salt: str | None = None) -> str:
    """生成 $6$ SHA-512 crypt 哈希。优先标准库 crypt，回退 openssl passwd -6。"""
    if salt is None:
        alphabet = string.ascii_letters + string.digits + "./"
        salt = "".join(secrets.choice(alphabet) for _ in range(16))
    spec = "$6$" + salt + "$"
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import crypt  # Python <=3.12；3.13 起移除
        return crypt.crypt(password, spec)
    except (ImportError, AttributeError):
        pass
    import subprocess
    r = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, password],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _read_lines(root: Path, rel: str) -> list[str]:
    p = root / rel
    if not p.exists():
        return []
    _refuse_symlink(root, rel)
    return p.read_text().splitlines()


def _refuse_symlink(root: Path, rel: str) -> None:
    # 逐段检查，防止通过符号链接穿透到宿主文件系统
    cur = root
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise SystemExit(f"拒绝写入穿透符号链接: {rel}")


def _write(root: Path, rel: str, text: str) -> None:
    _refuse_symlink(root, rel)
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    try:
        p.chmod(0o644 if not rel.endswith("shadow") and not rel.endswith("gshadow") else 0o640)
    except OSError:
        pass


def upsert_account_line(lines: list[str], key: str, newline: str) -> list[str]:
    out = [ln for ln in lines if not (ln and ln.split(":")[0] == key)]
    out.append(newline)
    return out


def set_shadow_password(lines: list[str], key: str, hashed: str) -> tuple[list[str], bool]:
    """把 <key> 的密码字段替换为 hashed；不存在则新建标准行。返回 (新行, 是否存在)."""
    for i, ln in enumerate(lines):
        parts = ln.split(":")
        if parts and parts[0] == key:
            parts[1] = hashed
            lines[i] = ":".join(parts)
            return lines, True
    # 新建：lastchg 用固定纪元天数，避免每次构建产生随机差异
    lines.append(f"{key}:{hashed}:19800:0:99999:7:::")
    return lines, False


def add_to_groups(lines: list[str], groups: tuple[str, ...], user: str) -> list[str]:
    for i, ln in enumerate(lines):
        parts = ln.split(":")
        if parts and parts[0] in groups:
            members = [m for m in (parts[3].split(",") if len(parts) > 3 and parts[3] else []) if m]
            if user not in members:
                members.append(user)
            while len(parts) < 4:
                parts.append("")
            parts[3] = ",".join(members)
            lines[i] = ":".join(parts)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rootfs", type=Path, required=True, help="已解出的 rootfs 目录树")
    ap.add_argument("--profile", type=Path, required=True, help="port-profile.json")
    ap.add_argument("--salt", default=None, help="固定盐（仅自检用，保证哈希可复现）")
    ap.add_argument("--no-autologin", action="store_true", help="不写 GDM 自动登录")
    a = ap.parse_args()

    root = a.rootfs.resolve()
    if not root.is_dir():
        raise SystemExit(f"rootfs 不是目录: {root}")
    if not (root / "etc").is_dir():
        raise SystemExit("缺少 etc/，看起来不是完整 rootfs")
    for rel in PROTECTED_TARGETS:
        if (root / rel).parent.is_symlink():
            raise SystemExit(f"拒绝写入穿透符号链接目录: {rel}")

    profile = json.loads(a.profile.read_text())
    login = profile.get("login", {})
    user = login.get("user", "piano")
    password = login.get("password", "piano")
    root_password = login.get("root_password", "piano")
    autologin = bool(login.get("autologin", True)) and not a.no_autologin

    user_hash = make_sha512_crypt(password, a.salt)
    root_hash = make_sha512_crypt(root_password, (a.salt + "root")[:16] if a.salt else None)
    for label, h in (("user", user_hash), ("root", root_hash)):
        if not h.startswith("$6$"):
            raise SystemExit(f"{label} 密码哈希不是 sha512-crypt: {h[:12]}")

    written: list[str] = []

    # 1) /etc/passwd —— 确保 piano 用户（uid/gid 1000）
    passwd = _read_lines(root, "etc/passwd")
    passwd = upsert_account_line(
        passwd, user,
        f"{user}:x:1000:1000:Piano:/home/{user}:/bin/bash")
    _write(root, "etc/passwd", "\n".join(passwd) + "\n"); written.append("etc/passwd")

    # 2) /etc/shadow —— piano 与 root 都设为可登录密码
    shadow = _read_lines(root, "etc/shadow")
    shadow, _ = set_shadow_password(shadow, user, user_hash)
    shadow, root_existed = set_shadow_password(shadow, "root", root_hash)
    _write(root, "etc/shadow", "\n".join(shadow) + "\n"); written.append("etc/shadow")

    # 3) /etc/group —— piano 私有组 + 加入管理/硬件组
    group = _read_lines(root, "etc/group")
    group = upsert_account_line(group, user, f"{user}:x:1000:")
    group = add_to_groups(group, ("sudo", "adm", "audio", "video", "plugdev",
                                  "users", "netdev", "input", "render", "dialout"), user)
    _write(root, "etc/group", "\n".join(group) + "\n"); written.append("etc/group")

    # 4) /etc/gshadow —— 与 group 对齐（存在才改，避免凭空创建不一致文件）
    gshadow_path = root / "etc/gshadow"
    if gshadow_path.exists():
        gshadow = _read_lines(root, "etc/gshadow")
        gshadow = upsert_account_line(gshadow, user, f"{user}:!::")
        _write(root, "etc/gshadow", "\n".join(gshadow) + "\n"); written.append("etc/gshadow")

    # 5) sudoers —— 确保 sudo 组免密策略之外的普通 sudo（密码=piano，与登录一致）
    sudoers = (root / "etc/sudoers.d")
    sudoers.mkdir(parents=True, exist_ok=True)
    _write(root, "etc/sudoers.d/10-piano-port",
           f"# SunUEFI port: {user} uses the same password for login and sudo.\n"
           f"%sudo ALL=(ALL:ALL) ALL\n")
    written.append("etc/sudoers.d/10-piano-port")

    # 6) GDM 自动登录（Ubuntu 26.04 使用 gdm3）
    if autologin:
        conf_rel = "etc/gdm3/custom.conf"
        conf = (root / conf_rel)
        text = conf.read_text() if conf.exists() and not conf.is_symlink() else ""
        # 幂等：移除旧的 AutomaticLogin* 行后再插入 [daemon] 段
        kept = [ln for ln in text.splitlines()
                if not ln.strip().startswith(("AutomaticLoginEnable", "AutomaticLogin="))]
        if "[daemon]" in kept:
            idx = kept.index("[daemon]")
            kept[idx + 1:idx + 1] = ["AutomaticLoginEnable=True", f"AutomaticLogin={user}"]
        else:
            kept = ["[daemon]", "AutomaticLoginEnable=True", f"AutomaticLogin={user}"] + kept
        _write(root, conf_rel, "\n".join(kept) + "\n"); written.append(conf_rel)

    # 7) 确保 home 目录骨架存在（不复制 skel 内容，交给首次登录 session）
    home = root / "home" / user
    if not home.is_symlink():
        home.mkdir(parents=True, exist_ok=True)

    report = {
        "status": "LOGIN_INJECTED_HOST_ONLY",
        "rootfs": str(root),
        "user": user,
        "user_password": password,
        "root_password": root_password,
        "autologin": autologin,
        "hash_method": "sha512-crypt ($6$)",
        "root_shadow_entry_existed": root_existed,
        "written": written,
        "note": "后处理注入，未改上游源码；须在源仓库 build-rootfs-image.py 的校验之后、生成最终 sparse 镜像之前执行。",
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
