#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""离线自检：用合成输入真实跑通全部桥接脚本并校验契约。

不依赖真机/内核编译/网络。验证：
  - port-stage-esp.py：ESP 布局与 SUNUEFI-LINUXv2 header（逐字段）
  - port-adapt-rootfs.py：fstab / root-policy / udev 规则
  - port-partition-plan.py：分区计划（不动 userdata）
  - port-inject-login.py：piano/piano + root piano（crypt 反算）+ 幂等 + 自动登录  [需求5]
  - port-package-artifacts.py：三产物目录 + Android sparse magic/chunk + sha256  [需求4]
  - port-release-assets.py：三产物→同名 ZIP + 全局 SHA256 + manifest + Notes + 缺产物负例  [需求7]
  - 发布门禁：真实产物 GUARD_CLEAR / 占位 GUARD_TRIGGERED + release-flags.env  [需求7 补强]
  - port-check-isolation.py：正例通过、上游改动触及命名空间负例被拦截  [需求3]
  - port-full-build.sh --dry-run：上游元数据/内核 commit/fragments/输出契约  [真实编译接线]
  - port-upstream-release.py adapt：上游 Release 成品 -> UEFI 输入（解 Android Boot Image、
    从 vendor_boot 抽真 initramfs、本机固件四件套 ESP、真实 Module ZIP、port-inputs.env
    契约）[需求 3 上游活水]

参数：仓库根（扁平布局含 port/... 与 .github/workflows/...；也兼容旧工程包 overlay/ 布局）。
"""
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

MAGIC = 0xed26ff3a
RAW, FILL = 0xCAC1, 0xCAC2


def sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def cpio_newc(files) -> bytes:
    """最小 newc cpio 归档，files 为 (name, data, mode) 列表。

    selftest 用它伪造上游 initramfs：port-native-esp.py 必须能在 /init 之上做 shim，
    所以输入得是能被 cpio 解析的真实归档，不能是随便一段字节。
    """
    def header(ino, mode, size, namesize):
        fields = (ino, mode, 0, 0, 1, 0, size, 0, 0, 0, 0, namesize, 0)
        return b"070701" + b"".join(b"%08x" % f for f in fields)

    out = bytearray()
    ino = 1
    for name, data, mode in files:
        nb = name.encode() + b"\0"
        hdr = header(ino, mode, len(data), len(nb)) + nb
        out += hdr + b"\0" * ((4 - len(hdr) % 4) % 4)
        out += data + b"\0" * ((4 - len(data) % 4) % 4)
        ino += 1
    nb = b"TRAILER!!!\0"
    hdr = header(0, 0, 0, len(nb)) + nb
    out += hdr + b"\0" * ((4 - len(hdr) % 4) % 4)
    return bytes(out)


def locate(kit: Path):
    """返回 (portroot, profile, distros, sync, tools)。兼容扁平仓库与旧工程包 KIT。"""
    if (kit / "overlay/port/tools").is_dir():
        portroot = kit / "overlay/port"
        wf = kit / "overlay/.github/workflows"
    elif (kit / "port/tools").is_dir():
        portroot = kit / "port"
        wf = kit / ".github/workflows"
    else:
        raise SystemExit(f"找不到 port/tools：{kit}")
    cfg = portroot / "config"
    return (portroot, cfg / "port-profile.json", cfg / "distros.json",
            cfg / "upstream-sync.json", portroot / "tools", wf)


def run(script: Path, *args, expect_fail=False):
    r = subprocess.run([sys.executable, str(script), *[str(a) for a in args]],
                       capture_output=True, text=True)
    if expect_fail:
        return r
    if r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        raise SystemExit(f"脚本执行失败: {script.name}: {r.stderr.strip()}")
    return r.stdout


def make_synthetic_inputs(d: Path):
    image = b"Linux version 7.2.14-piano56 (test) " + b"\x00" * (128 * 1024)
    body = b"\xd0\x0d\xfe\xed" + struct.pack(">I", 48) + b"\x00" * 40
    initrd = b"\x1f\x8b\x08SYNTHETIC-CPIO" + b"\x00" * 2048
    (d / "Image").write_bytes(image)
    (d / "piano-boot.dtb").write_bytes(body)
    (d / "initramfs.cpio.gz").write_bytes(initrd)
    return image, body, initrd


def verify_password(hashed: str, password: str) -> bool:
    """用 crypt 或 openssl 反算校验 sha512-crypt 哈希。"""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import crypt as _crypt
        return _crypt.crypt(password, hashed) == hashed
    except (ImportError, AttributeError):
        r = subprocess.run(["openssl", "passwd", "-6", "-salt",
                            hashed.split("$")[2], password],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip() == hashed


def main() -> int:
    kit = Path(sys.argv[1]).resolve()
    portroot, profile, distros, sync, tools, wf = locate(kit)
    prof = json.loads(profile.read_text())
    rid = prof["partitions"]["root"]["partuuid"]

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        image, dtb, initrd = make_synthetic_inputs(d)

        # ---- 1. ESP staging ----
        esp = d / "esp"
        run(tools / "port-stage-esp.py", "--image", d / "Image",
            "--dtb", d / "piano-boot.dtb", "--initrd", d / "initramfs.cpio.gz",
            "--profile", profile, "--output", esp)
        stable = esp / "EFI" / "Piano" / "stable"
        assert (stable / "Image.efi").read_bytes() == image
        assert (stable / "piano.dtb").read_bytes() == dtb
        assert (stable / "initramfs.cpio.gz").read_bytes() == initrd
        cfg = (stable / "config").read_text()
        assert f"root=PARTUUID={rid}" in cfg and f"piano.root=PARTUUID={rid}" in cfg
        payload = (stable / "boot.img").read_bytes()
        magic, ver, hsize, klen, ilen, ksha, isha, dlen, dsha = struct.unpack_from(
            "<16sIIQQ32s32sQ32s", payload, 0)
        assert magic == b"SUNUEFI-LINUXv2\0" and ver == 2 and hsize == 144
        assert klen == len(image) and ilen == len(initrd) and dlen == len(dtb)
        assert ksha == sha(image) and isha == sha(initrd) and dsha == sha(dtb)
        assert payload[144:] == image + initrd + dtb
        m = json.loads((esp / "port-esp-manifest.json").read_text())
        assert m["status"] == "HOST_STAGED_NOT_FLASHED" and m["payload"]["sha256"] == sha(payload).hex()
        print("  [stage-esp] ESP 布局 + SUNUEFI-LINUXv2 header 逐字段 OK")

        # ---- 2. rootfs adapt ----
        rootfs = d / "rootfs"
        (rootfs / "etc").mkdir(parents=True)
        (rootfs / "bin").mkdir()
        # 预置上游风格的账号文件（无普通用户 + root 锁定），验证注入能覆盖
        (rootfs / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1::/usr/sbin:/usr/sbin/nologin\n")
        (rootfs / "etc/shadow").write_text("root:*:19800:0:99999:7:::\ndaemon:*:19800:0:99999:7:::\n")
        (rootfs / "etc/group").write_text("root:x:0:\nsudo:x:27:\naudio:x:29:\n")
        (rootfs / "etc/gshadow").write_text("root:*::\nsudo:*::\n")
        run(tools / "port-adapt-rootfs.py", "--rootfs", rootfs, "--profile", profile,
            "--legacy-home-uuid", "11111111-2222-3333-4444-555555555555")
        fstab = (rootfs / "etc/fstab").read_text()
        assert f"PARTUUID={rid}  /" in fstab and "/boot/efi" in fstab
        assert "11111111-2222" in fstab
        policy = json.loads((rootfs / "etc/piano/root-policy.json").read_text())
        assert policy["mode"] == "partition" and policy["root_partuuid"] == rid
        assert "blockdev --setro" in (rootfs / "etc/udev/rules.d/01-piano-protect-android.rules").read_text()
        print("  [adapt-rootfs] fstab / root-policy / udev 保护规则 OK")

        # ---- 3. partition plan ----
        planout = d / "plan"
        run(tools / "port-partition-plan.py", "--profile", profile, "--output", planout)
        plan = json.loads((planout / "partition-plan.json").read_text())
        assert plan["android_data_cleared"] is False
        assert [p["name"] for p in plan["new_partitions"]] == ["sunuefi_esp", "sunuefi_root"]
        assert "userdata" in plan["untouched"]
        assert "set-active=b" in (planout / "partition-commands.txt").read_text()
        print("  [partition-plan] 分区计划（不动 userdata，含安全红线）OK")

        # ---- 4. 账号注入（需求5）----
        run(tools / "port-inject-login.py", "--rootfs", rootfs, "--profile", profile,
            "--salt", "SunUEFIPort")
        shadow = {ln.split(":")[0]: ln.split(":")[1]
                  for ln in (rootfs / "etc/shadow").read_text().splitlines() if ln}
        assert shadow.get("root", "").startswith("$6$"), "root 密码未解锁/未设置"
        assert shadow.get("piano", "").startswith("$6$"), "piano 用户未创建"
        assert verify_password(shadow["root"], "piano"), "root 密码不是 piano"
        assert verify_password(shadow["piano"], "piano"), "piano 密码不是 piano"
        passwd_users = [ln.split(":")[0] for ln in (rootfs / "etc/passwd").read_text().splitlines() if ln]
        assert "piano" in passwd_users and passwd_users.count("piano") == 1
        assert "AutomaticLogin=piano" in (rootfs / "etc/gdm3/custom.conf").read_text()
        assert "%sudo" in (rootfs / "etc/sudoers.d/10-piano-port").read_text()
        # 幂等：再跑一次不产生重复行
        run(tools / "port-inject-login.py", "--rootfs", rootfs, "--profile", profile,
            "--salt", "SunUEFIPort")
        assert (rootfs / "etc/passwd").read_text().splitlines().count(
            next(l for l in (rootfs / "etc/passwd").read_text().splitlines() if l.startswith("piano:"))) == 1
        print("  [inject-login] piano/piano + root=piano + 自动登录 + 幂等 OK")

        # ---- 5. 三产物打包（需求4，含真 sparse 编码）----
        # 合成 4K 对齐的 raw 镜像（含零块与非零块，触发 RAW+FILL 两种 chunk）
        blk = 4096
        esp_raw = d / "esp.raw"
        esp_raw.write_bytes(b"\x55" * blk + b"\x00" * (blk * 3) + b"\xAA" * blk)
        root_raw = d / "root.raw"
        root_raw.write_bytes(b"\x00" * blk + b"\xEE" * (blk * 2))
        module_zip = d / "module.zip"
        module_zip.write_bytes(b"PK\x03\x04SYNTHETIC-ZIP")
        arts = d / "artifacts"
        run(tools / "port-package-artifacts.py", "--profile", profile, "--distros", distros,
            "--output", arts, "--module-zip", module_zip,
            "--esp-image", esp_raw, "--root-image", root_raw, "--version", "9.9.9-test")
        am = json.loads((arts / "manifest.json").read_text())
        assert am["status"] == "ARTIFACTS_PACKAGED_HOST_ONLY"
        assert "SunUEFI-Module" in am["artifacts"] and "SunUEFI-ESP" in am["artifacts"]
        roots = [k for k in am["artifacts"] if k.startswith("SunUEFI-Root-")]
        assert roots, "缺 SunUEFI-Root- 产物"
        # 严格命名
        assert (arts / "SunUEFI-Module" / "SunUEFI-Piano-9.9.9-test.zip").is_file()
        esp_img = arts / "SunUEFI-ESP" / "esp.sparse.img"
        root_img = arts / roots[0] / f"root-{roots[0].replace('SunUEFI-Root-','')}.sparse.img"
        assert esp_img.is_file() and root_img.is_file()
        # 校验 sparse header + 逐 chunk 展开一致性
        for img, raw in ((esp_img, esp_raw), (root_img, root_raw)):
            data = img.read_bytes()
            mg, ver, m0, hsz, csz, blksz, blocks, chunks, csum = struct.unpack_from("<IHHHHIIII", data, 0)
            assert mg == MAGIC and ver == 1 and hsz == 28 and csz == 12 and blksz == blk
            assert blocks == raw.stat().st_size // blk
            # 展开所有 chunk，回读应与 raw 完全一致
            off = hsz
            rebuilt = bytearray()
            for _ in range(chunks):
                kind, _, count, total = struct.unpack_from("<HHII", data, off)
                body = data[off + csz: off + total]
                if kind == RAW:
                    assert len(body) == count * blk
                    rebuilt += body
                elif kind == FILL:
                    rebuilt += b"\x00" * (count * blk)
                else:
                    raise AssertionError(f"未知 chunk kind {kind:#x}")
                off += total
            assert bytes(rebuilt) == raw.read_bytes(), "sparse 展开与 raw 不一致"
            row = am["artifacts"][img.parent.name]["image"]
            assert row["expanded_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
            assert row["sha256"] == hashlib.sha256(data).hexdigest()
        # SHA256SUMS 存在且格式对
        sums = (arts / "SunUEFI-ESP" / "SHA256SUMS").read_text()
        assert len(sums.splitlines()[0].split()[0]) == 64
        print("  [package-artifacts] 三产物目录 + sparse magic/chunk/回读 + sha256 OK")

        # ---- 6. Release 资产汇总（需求 7）----
        import zipfile
        rel_out = d / "release-assets"
        run(tools / "port-release-assets.py", "--artifacts", arts,
            "--out", rel_out, "--tag", "v9.9.9-selftest",
            "--policy", portroot / "config" / "release-policy.json",
            "--repo", "owner/repo", "--run-id", "1", "--commit", "abc123", "--json")
        zips = sorted(p.name for p in rel_out.glob("SunUEFI-*.zip"))
        assert len(zips) == 3, f"应有 3 个产物 ZIP，实际 {zips}"
        assert "SunUEFI-Module.zip" in zips and "SunUEFI-ESP.zip" in zips
        assert any(z.startswith("SunUEFI-Root-") for z in zips)
        # 每个 ZIP 内容与原产物目录逐字节一致（含各目录自己的 SHA256SUMS）
        for zp in sorted(rel_out.glob("SunUEFI-*.zip")):
            src_dir = arts / zp.stem
            expected = {p.relative_to(src_dir).as_posix()
                        for p in src_dir.rglob("*") if p.is_file()}
            with zipfile.ZipFile(zp) as z:
                assert set(z.namelist()) == expected, f"{zp.name} 内文件与源目录不一致"
                for n in expected:
                    assert z.read(n) == (src_dir / n).read_bytes(), f"{zp.name}:{n} 内容不一致"
        # 全局 SHA256SUMS：所有 ZIP + manifest.json，逐个反算
        sums_lines = (rel_out / "SHA256SUMS").read_text().splitlines()
        assert len(sums_lines) == len(zips) + 1
        for line in sums_lines:
            h, name = line.split("  ", 1)
            assert h == hashlib.sha256((rel_out / name).read_bytes()).hexdigest(), name
        # manifest 关键字段
        rm = json.loads((rel_out / "manifest.json").read_text())
        assert rm["tag"] == "v9.9.9-selftest" and rm["repo"] == "owner/repo"
        assert rm["upstream"]["branch"] == "main"
        assert set(rm["artifacts"]) == {"SunUEFI-Module", "SunUEFI-ESP", roots[0]}
        assert rm["status"] in ("RELEASE_ASSETS_READY",
                                "RELEASE_ASSETS_CONTAINS_PLACEHOLDERS")
        # RELEASE-NOTES.md 含 tag、三个 zip 名、校验命令
        notes = (rel_out / "RELEASE-NOTES.md").read_text()
        assert "v9.9.9-selftest" in notes and "sha256sum -c SHA256SUMS" in notes
        for z in zips:
            assert z in notes, f"Notes 缺 {z}"
        # upload-files.txt 列出的文件都存在且完整
        upl = (rel_out / "upload-files.txt").read_text().splitlines()
        assert set(upl) == set(zips) | {"manifest.json", "SHA256SUMS", "RELEASE-NOTES.md"}
        for n in upl:
            assert (rel_out / n).is_file()
        # 负例：缺 ESP 且不带 --allow-partial，发布必须被拒绝
        partial = d / "partial"
        (partial / "SunUEFI-Module").mkdir(parents=True)
        (partial / "SunUEFI-Module" / "README.txt").write_text("placeholder")
        r3 = subprocess.run(
            [sys.executable, str(tools / "port-release-assets.py"),
             "--artifacts", str(partial), "--out", str(d / "rel_partial"),
             "--tag", "v0", "--policy", str(portroot / "config" / "release-policy.json")],
            capture_output=True, text=True)
        assert r3.returncode != 0 and "缺少必需产物" in (r3.stdout + r3.stderr), \
            "缺 ESP 时未拒绝发布"
        # 发布门禁：本段用的是真实合成镜像 -> GUARD_CLEAR，flags 全 0
        flags_path = rel_out / "release-flags.env"
        assert flags_path.is_file(), "缺 release-flags.env"
        flags = dict(line.split("=", 1) for line in flags_path.read_text().splitlines()
                     if "=" in line)
        assert flags["FORCE_DRAFT"] == "0" and flags["FORCE_PRERELEASE"] == "0"
        assert flags["GUARD_STATUS"] == "GUARD_CLEAR"
        assert rm["publish_guard"]["status"] == "GUARD_CLEAR"
        assert rm["publish_guard"]["force_draft"] is False
        assert "release-flags.env" not in upl
        assert "release-flags.env" not in "\n".join(sums_lines)
        # 占位场景：三个目录只有 README.txt -> 门禁必须触发
        ph = d / "placeholder_arts"
        for name in ("SunUEFI-Module", "SunUEFI-ESP", "SunUEFI-Root-ubuntu-gnome"):
            (ph / name).mkdir(parents=True)
            (ph / name / "README.txt").write_text("placeholder\n")
        ph_out = d / "placeholder_release"
        run(tools / "port-release-assets.py", "--artifacts", ph,
            "--out", ph_out, "--tag", "v0.0.0-placeholder",
            "--policy", portroot / "config" / "release-policy.json", "--json")
        ph_man = json.loads((ph_out / "manifest.json").read_text())
        assert ph_man["status"] == "RELEASE_ASSETS_CONTAINS_PLACEHOLDERS"
        assert ph_man["publish_guard"]["status"] == "GUARD_TRIGGERED"
        assert ph_man["publish_guard"]["force_draft"] is True
        ph_flags = (ph_out / "release-flags.env").read_text()
        assert "FORCE_DRAFT=1" in ph_flags and "FORCE_PRERELEASE=1" in ph_flags
        assert "GUARD_STATUS=GUARD_TRIGGERED" in ph_flags
        assert "发布门禁已生效" in (ph_out / "RELEASE-NOTES.md").read_text()
        assert "release-flags.env" not in (ph_out / "upload-files.txt").read_text()
        print("  [release-assets] 三 ZIP + 全局 SHA256 + manifest + Notes + 缺产物负例 OK")
        print("  [release-guard] 真实产物 GUARD_CLEAR / 占位 GUARD_TRIGGERED + flags OK")

        # ---- 6b. Release 大资产分卷（超过单资产 2 GiB 上限，用小阈值离线覆盖）----
        import os
        split_arts = d / "split_arts"
        for name in ("SunUEFI-Module", "SunUEFI-ESP", "SunUEFI-Root-ubuntu-gnome"):
            (split_arts / name).mkdir(parents=True)
            (split_arts / name / "SHA256SUMS").write_text("0  placeholder\n")
        big = os.urandom(5200)
        (split_arts / "SunUEFI-ESP" / "esp.sparse.img").write_bytes(big)
        (split_arts / "SunUEFI-Root-ubuntu-gnome" /
         "root-ubuntu-gnome.sparse.img").write_bytes(big)
        split_out = d / "split_release"
        run(tools / "port-release-assets.py", "--artifacts", split_arts,
            "--out", split_out, "--tag", "v0.0.0-split",
            "--policy", portroot / "config" / "release-policy.json",
            "--max-asset-bytes", "1000", "--no-compress", "--json")
        sm = json.loads((split_out / "manifest.json").read_text())
        parted = {n: r for n, r in sm["artifacts"].items() if r["parted"]}
        assert len(parted) == 2, f"应有两个产物走分卷: {list(parted)}"
        parts = list(split_out.glob("*.part*"))
        assert parts and all(p.stat().st_size <= 1000 for p in parts), "分卷片超阈值"
        stem = "SunUEFI-Root-ubuntu-gnome--root-ubuntu-gnome.sparse.img"
        group = sorted(split_out.glob(stem + ".part*"),
                       key=lambda p: int(p.name.rsplit("part", 1)[1]))
        assert b"".join(p.read_bytes() for p in group) == big, "分卷还原不一致"
        assert "还原分卷镜像" in (split_out / "RELEASE-NOTES.md").read_text()
        sums = (split_out / "SHA256SUMS").read_text()
        upl = (split_out / "upload-files.txt").read_text().splitlines()
        assert all(n in sums for n in upl
                   if n not in ("SHA256SUMS", "RELEASE-NOTES.md")), "SHA256 未覆盖分卷"
        print("  [release-assets-split] 超上限裸切分卷 + cat 还原 + SHA256 覆盖 OK")

        # ---- 6c. 大镜像 zstd 压缩（有 zstd 才测；缺工具时应自动退回裸切）----
        import shutil as _sh
        has_zstd = bool(_sh.which("zstd"))
        if not has_zstd:
            try:
                import zstandard  # noqa: F401
                has_zstd = True
            except ImportError:
                pass
        if has_zstd:
            z_out = d / "zstd_release"
            run(tools / "port-release-assets.py", "--artifacts", split_arts,
                "--out", z_out, "--tag", "v0.0.0-zstd",
                "--policy", portroot / "config" / "release-policy.json",
                "--max-asset-bytes", "3000", "--json")
            zparts = sorted(z_out.glob("*.sparse.img.zst*"))
            assert zparts, "未生成 .zst 资产"
            assert any(p.name.endswith(".zst") for p in zparts) or \
                   any(".zst.part" in p.name for p in zparts)
            notes = (z_out / "RELEASE-NOTES.md").read_text()
            assert "zstd -d" in notes, "还原命令必须是 zstd -d"
            print("  [release-assets-zstd] 大镜像 zstd 压缩再分卷 + 还原命令 OK")
        else:
            print("  [release-assets-zstd] 跳过（环境无 zstd，脚本会退回裸切）")

        # ---- 7. port 层自包含校验（独立仓库模型，正例 + 两个负例）----
        iso_script = tools / "port-check-isolation.py"
        # 造一个「独立 port 仓库」的 mock 树：只有 port/ + sunuefi-* workflow
        mock = d / "mock_standalone"
        (mock / "port").mkdir(parents=True)
        import shutil
        for sub in ("config", "tools", "build", "docs"):
            shutil.copytree(portroot / sub, mock / "port" / sub)
        (mock / ".github/workflows").mkdir(parents=True)
        if wf.is_dir():
            for y in wf.glob("sunuefi-*.yml"):
                shutil.copy(y, mock / ".github/workflows" / y.name)
        (mock / "README.md").write_text("port\n")
        r = run(iso_script, "--root", mock, "--mode", "tree", "--json")
        rep = json.loads(r)
        assert rep["status"] == "OK", f"正例自包含校验失败: {rep['violations']}"
        assert rep["model"] == "standalone-port-repo"
        # 负例 1：加一个非 sunuefi- 前缀的 workflow，应被拦截
        (mock / ".github/workflows/build.yml").write_text("name: clash\n")
        r2 = subprocess.run([sys.executable, str(iso_script), "--root", str(mock),
                             "--mode", "tree", "--json"], capture_output=True, text=True)
        rep2 = json.loads(r2.stdout)
        assert rep2["status"] == "VIOLATION" and r2.returncode == 1, "负例1未被拦截"
        assert any("sunuefi- 前缀" in v or "撞名" in v for v in rep2["violations"])
        # 负例 2：独立仓库混入上游源码目录，应被拦截
        mock2 = d / "mock_smuggled"
        shutil.copytree(mock, mock2)
        (mock2 / ".github/workflows/build.yml").unlink()
        (mock2 / "kernel").mkdir(parents=True)
        (mock2 / "kernel/source.json").write_text("{}")
        r3 = subprocess.run([sys.executable, str(iso_script), "--root", str(mock2),
                             "--mode", "tree", "--json"], capture_output=True, text=True)
        rep3 = json.loads(r3.stdout)
        assert rep3["status"] == "VIOLATION" and r3.returncode == 1, "负例2未被拦截"
        assert any("上游源码目录" in v for v in rep3["violations"]), rep3["violations"]
        print("  [isolation] 独立仓库自包含正例 / 非前缀 workflow / 混入上游源码 两负例 OK")

        # ---- 8. 真实编译接线器 port-full-build.sh --dry-run（离线验证契约）----
        fb_fork = d / "fb_fork"
        (fb_fork / "kernel").mkdir(parents=True)
        (fb_fork / "port" / "config").mkdir(parents=True)
        (fb_fork / "kernel" / "source.json").write_text(json.dumps({
            "url": "https://example.invalid/linux-sm8750-piano.git",
            "commit": "bf42598aceec58fd7392314de233e5fab3835938",
            "branch": "piano/virt-1.0",
            "config": "arch/arm64/configs/piano-diagnostic.config",
            "release_config_fragments": ["arch/arm64/configs/piano-diagnostic.config",
                                         "arch/arm64/configs/piano-release.config"],
            "kernel_release": "7.2.14-piano56"}))
        shutil.copy(profile, fb_fork / "port" / "config" / "port-profile.json")
        fb_inputs = d / "fb_inputs"
        fb_inputs.mkdir()
        fb_script = portroot / "build" / "port-full-build.sh"
        if sys.platform == "win32":
            # MSYS/Git Bash 的 pwd 返回 /tmp/... 形式，脚本把该字符串直接嵌进
            # `python3 -c`，Windows 原生 python 解析不了这个路径。CI 是 Linux，
            # 这条用例在 CI 上照常执行；本地 Windows 只跳过。
            print("  [full-build] 跳过（Windows+MSYS 的 /tmp 路径无法传给原生 python）")
        else:
            r4 = subprocess.run(["bash", str(fb_script), "--fork", str(fb_fork),
                                 "--inputs", str(fb_inputs), "--dry-run"],
                                capture_output=True, text=True)
            assert r4.returncode == 0, f"port-full-build.sh --dry-run 失败: {r4.stderr}"
            assert "config fragments = 2" in r4.stdout
            plan = json.loads((fb_inputs / "kernel-plan.json").read_text())
            assert plan["kernel_release"] == "7.2.14-piano56"
            assert plan["commit"] == "bf42598aceec58fd7392314de233e5fab3835938"
            fb_env = (fb_inputs / "port-inputs.env").read_text()
            assert "FULL_BUILD_STATUS=DRY_RUN" in fb_env
            assert "ESP_IMAGE=" in fb_env and "ROOT_IMAGE=" in fb_env
            print("  [full-build] --dry-run 元数据/源码契约/输出契约 OK")

        # ---- 9. 上游 Release 适配器（默认构建路径，纯 Python 离线覆盖）----
        # 不依赖 zstd/mtools：只验证 adapt 的解包/ESP/Module/契约逻辑。
        import gzip
        import zipfile
        adapter = tools / "port-upstream-release.py"
        # KIT 模式 tests/ 在套件根；落地树模式 tests/ 在 port/ 内
        fixture_gen = portroot / "tests" / "make_upstream_fixture.py"
        if not fixture_gen.is_file():
            fixture_gen = kit / "tests" / "make_upstream_fixture.py"
        if adapter.is_file():
            ur_fork = d / "ur_fork"
            shutil.copytree(portroot, ur_fork / "port")
            ur_inputs = d / "ur_inputs"
            ur_inputs.mkdir()
            tag = "v1.0.0-selftest"
            # 合成上游 boot.zip（Android Boot Image v2 + 完整合并 DTB）
            import os
            kernel = bytearray(128 * 1024)
            kernel[:40] = b"Linux version 7.2.14-piano56 selftest\x00"[:40]
            kernel[0:2] = b"MZ"          # port-native-esp.py 要求 EFI Stub PE 头
            # 真实 newc cpio（含 /init）：port-native-esp.py 要在 /init 之上挂 shim，
            # 且注入后重新打包，所以必须是可解析的归档而不是任意字节。
            ramdisk_raw = cpio_newc([
                ("init", b"#!/bin/sh\nexec /bin/sh\n", 0o100755),
                ("etc", b"", 0o040755),
                ("etc/hostname", b"piano\n", 0o100644),
                ("big", os.urandom(20000), 0o100644),
            ])
            ramdisk = gzip.compress(ramdisk_raw, mtime=0)
            assert len(ramdisk) >= 4096, "合成 ramdisk 太小，会被判为空包"
            page, hsize = 4096, 1660
            bh = bytearray(hsize)
            bh[0:8] = b"ANDROID!"
            struct.pack_into("<I", bh, 8, len(kernel))
            struct.pack_into("<I", bh, 16, len(ramdisk))
            struct.pack_into("<I", bh, 36, page)
            struct.pack_into("<I", bh, 40, 2)
            struct.pack_into("<I", bh, 1644, hsize)
            boot = bytearray(page)
            boot[:hsize] = bh
            boot += kernel
            boot += b"\x00" * ((page - len(boot) % page) % page)
            boot += ramdisk
            boot += b"\x00" * ((page - len(boot) % page) % page)
            dtb = bytearray(256)
            dtb[0:4] = b"\xd0\x0d\xfe\xed"
            struct.pack_into(">I", dtb, 4, len(dtb))
            with zipfile.ZipFile(ur_inputs / f"piano-ubuntu-{tag}-boot.zip", "w") as z:
                z.writestr("boot/boot.img", bytes(boot))
                z.writestr("boot/piano-boot.dtb", bytes(dtb))
            (ur_inputs / "upstream-release.json").write_text(json.dumps({
                "schema": 1, "source": "upstream-release", "tag": tag,
                "repo": "yzddmr6/xiaomipad-8pro-mainline",
                "target_commitish": "selftest-commit"}))
            r5 = subprocess.run([sys.executable, str(adapter), "adapt",
                                 "--fork", str(ur_fork), "--inputs", str(ur_inputs),
                                 "--tag", tag], capture_output=True, text=True)
            assert r5.returncode == 0, f"adapt 失败: {r5.stderr}"
            assert (ur_inputs / "Image").read_bytes() == bytes(kernel)
            assert (ur_inputs / "initramfs.cpio.gz").read_bytes() == ramdisk
            assert (ur_inputs / "piano-boot.dtb").read_bytes() == bytes(dtb)
            env = (ur_inputs / "port-inputs.env").read_text()
            for token in ("SOURCE=upstream-release", f"UPSTREAM_TAG={tag}",
                          "DTB_STATUS=UPSTREAM_MERGED_DTB", "ESP_DIR=", "MODULE_ZIP="):
                assert token in env, f"port-inputs.env 缺 {token}"
            # 回归：env 必须能被 shell 直接 source。KERNEL_SOURCE 等值含空格，
            # 不加引号会让 workflow 的 `source` 把 "+" 当命令（exit 127）。
            chk = subprocess.run(
                ["bash", "-c",
                 'set -eu; . "$1"; test "$SOURCE" = upstream-release; '
                 'test -n "$KERNEL_SOURCE"; test -n "$MODULE_ZIP"',
                 "_", str(ur_inputs / "port-inputs.env")],
                capture_output=True, text=True)
            assert chk.returncode == 0, f"port-inputs.env 无法 source: {chk.stderr}"
            esp_manifest = json.loads(
                (ur_inputs / "esp-tree/native-esp-manifest.json").read_text())
            assert esp_manifest["layout"] == "native-stable-quad"
            stable = ur_inputs / "esp-tree/EFI/Piano/stable"
            assert (stable / "Image").read_bytes() == bytes(kernel)
            assert (stable / "board.dtb").read_bytes() == bytes(dtb)
            # boot.img 必须是 Android boot v2（本机固件按头长度定位 kernel/ramdisk），
            # 不能是 SUNUEFI-LINUXv2 那套自定义 144B 头。
            boot_bytes = (stable / "boot.img").read_bytes()
            assert boot_bytes[:8] == b"ANDROID!"
            assert struct.unpack_from("<I", boot_bytes, 40)[0] == 2
            assert struct.unpack_from("<I", boot_bytes, 1644)[0] == 1660
            modzip = ur_inputs / f"SunUEFI-Piano-{tag}.zip"
            assert modzip.is_file()
            with zipfile.ZipFile(modzip) as z:
                names = z.namelist()
                for req in ("module.prop", "service.sh", "payload/etc/shadow",
                            "payload/etc/fstab", "payload/etc/piano/root-policy.json",
                            "partition-plan.json"):
                    assert req in names, f"Module ZIP 缺 {req}"
                assert b"piano" in z.read("payload/etc/passwd")
            print("  [upstream-release] 解 Boot Image / ESP payload / 真实 Module / 契约 OK")

    print("\nSELFTEST OK —— 需求 1/2/3/4/5/7 + 上游 Release 接线 + 真实编译接线 + 发布门禁 全部离线跑通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
