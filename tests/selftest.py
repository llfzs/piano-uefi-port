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
  - port-check-isolation.py：正例通过、上游改动触及命名空间负例被拦截  [需求3]

参数：套件根 KIT（含 overlay/port/... 或已 apply 后含 port/...）。
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


def locate(kit: Path):
    """返回 (portroot, profile, distros, sync, tools)。兼容 KIT 与已 apply 的 fork。"""
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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import crypt as _crypt
    try:
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
        print("  [release-assets] 三 ZIP + 全局 SHA256 + manifest + Notes + 缺产物负例 OK")

        # ---- 7. 隔离校验（需求3，正例 + 负例）----
        iso_script = tools / "port-check-isolation.py"
        # 造一个「应用了移植层」的 mock 树
        mock = d / "mock_fork"
        (mock / "port").mkdir(parents=True)
        import shutil
        for sub in ("config", "tools", "build", "docs"):
            shutil.copytree(portroot / sub, mock / "port" / sub)
        (mock / ".github/workflows").mkdir(parents=True)
        if wf.is_dir():
            for y in wf.glob("sunuefi-*.yml"):
                shutil.copy(y, mock / ".github/workflows" / y.name)
        (mock / "kernel").mkdir(); (mock / "kernel/README").write_text("upstream")
        r = run(iso_script, "--root", mock, "--mode", "tree", "--json")
        rep = json.loads(r)
        assert rep["status"] == "OK", f"正例隔离失败: {rep['violations']}"
        # 负例：加一个非 sunuefi- 前缀的 workflow，应被拦截
        (mock / ".github/workflows/build.yml").write_text("name: clash\n")
        r2 = subprocess.run([sys.executable, str(iso_script), "--root", str(mock),
                             "--mode", "tree", "--json"], capture_output=True, text=True)
        rep2 = json.loads(r2.stdout)
        assert rep2["status"] == "VIOLATION" and r2.returncode == 1, "负例未被拦截"
        assert any("sunuefi- 前缀" in v or "撞名" in v for v in rep2["violations"])
        print("  [isolation] 正例通过、非前缀 workflow 负例被拦截 OK")

    print("\nSELFTEST OK —— 需求 1/2/3/4/5/7 的 port 层逻辑全部离线跑通")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
