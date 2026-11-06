#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：把 CI 三产物汇总成可发布到 GitHub Release 的资产（需求 7）。

输入：`actions/download-artifact` 或 `gh run download` 落地后的目录，其下是
  SunUEFI-Module/  SunUEFI-ESP/  SunUEFI-Root-<target>/
每个产物目录内部各自带一个 `SHA256SUMS`——若直接把目录内文件平铺上传，
Release 的同名 asset 会互相覆盖。因此本脚本把**每个产物目录压成同名 ZIP**：

  release-assets/
    SunUEFI-Module.zip            内含原目录全部文件（含其自身 SHA256SUMS）
    SunUEFI-ESP.zip
    SunUEFI-Root-<target>.zip
    manifest.json                 汇总清单（每个 zip 的 sha256、源文件、状态、构建元数据）
    SHA256SUMS                    全局校验和（针对所有 zip + manifest.json）
    RELEASE-NOTES.md              人类可读发布说明（含校验和、上游基线、安全提示）
    upload-files.txt              每行一个待上传文件名，供 gh release create/upload 消费

设计约束：
  * ZIP 用 STORED 模式 + 固定时间戳，避免数 GB 镜像压缩耗时，且产物可复现；
    流式写入，不把整个镜像读进内存。
  * 严格校验 Module / ESP / 至少一个 Root 齐全（--allow-partial 仅供离线自检）。
  * 只写 --out 目录，不碰上游受管目录、不联网、不调用 gh/fastboot。
"""
import argparse
import datetime
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as s:
        for chunk in iter(lambda: s.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_artifact_dirs(root: Path) -> list[Path]:
    """兼容两种落地形态：根下直接是 SunUEFI-*，或根下再包一层目录。"""
    def pick(base: Path) -> list[Path]:
        return sorted((p for p in base.iterdir()
                       if p.is_dir() and p.name.startswith("SunUEFI-")),
                      key=lambda p: p.name)

    found = pick(root)
    if found:
        return found
    for sub in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        found = pick(sub)
        if found:
            return found
    return []


def collect_files(src: Path) -> list[Path]:
    return sorted((p for p in src.rglob("*")
                   if p.is_file() and not p.is_symlink()
                   and "__pycache__" not in p.parts and p.suffix != ".pyc"),
                  key=lambda p: p.relative_to(src).as_posix())


def make_zip(src: Path, dst: Path) -> list[str]:
    """把产物目录内文件流式写入 ZIP；返回 zip 内文件名列表。"""
    files = collect_files(src)
    if not files:
        raise SystemExit(f"产物目录为空，拒绝发布空包: {src}")
    names = []
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for p in files:
            rel = p.relative_to(src).as_posix()
            zi = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            with z.open(zi, "w", force_zip64=True) as out, p.open("rb") as inp:
                shutil.copyfileobj(inp, out, 1024 * 1024)
            names.append(rel)
    return names


def artifact_status(names: list[str]) -> str:
    real = [n for n in names if n.endswith((".zip", ".img", ".sparse.img"))]
    if real:
        return "READY"
    if "README.txt" in names:
        return "PLACEHOLDER"
    return "UNKNOWN"


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


def build_notes(meta: dict, artifacts: dict, policy: dict, sums_text: str) -> str:
    lines = [
        f"# {meta['release_name']}",
        "",
        "> 由 GitHub Actions 自动构建并发布（`.github/workflows/sunuefi-build.yml` 的 `publish` job）。",
        "> UEFI 双系统移植层位于 `port/`，与上游 `xiaomipad-8pro-mainline` 零文件重叠，可随时同步。",
        "",
        "## 构建信息",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| Tag | `{meta['tag']}` |",
        f"| 仓库 | `{meta['repo'] or '(未指定)'}` |",
        f"| Commit | `{meta['commit'] or '(未指定)'}` |",
        f"| Actions run | `{meta['run_id'] or '(未指定)'}` |",
        f"| 构建时间 (UTC) | {meta['built_at_utc']} |",
        f"| 上游基线 | {meta['upstream']['url']} @ `{meta['upstream']['branch']}`"
        + (f" / {meta['upstream']['baseline']}" if meta['upstream']['baseline'] else "") + " |",
        f"| 发布状态 | `{meta['status']}` |",
        "",
        "## 产物",
        "",
        "每个 ZIP 内含该产物的全部文件（含其目录级 `SHA256SUMS`）；解压后即为 "
        "`port-package-artifacts.py` 产出的原始目录内容。",
        "",
        "| 文件 | 大小 | SHA256 | 状态 |",
        "|---|---|---|---|",
    ]
    for name, row in artifacts.items():
        lines.append(f"| `{row['zip']}` | {human(row['bytes'])} | `{row['sha256']}` | {row['status']} |")
    lines += [
        "",
        "| 附加文件 | 说明 |",
        "|---|---|",
        "| `manifest.json` | 汇总清单（各产物源文件、状态、校验和、构建元数据） |",
        "| `SHA256SUMS` | 全局校验和，覆盖所有 ZIP 与 `manifest.json` |",
        "| `RELEASE-NOTES.md` | 本文件 |",
        "",
    ]
    placeholders = [n for n, r in artifacts.items() if r["status"] == "PLACEHOLDER"]
    if placeholders and policy["notes"]["include_placeholder_warning"]:
        lines += [
            "> ⚠️ **这是 CI 骨架产物，不是可刷入的固件。** 以下产物目录里只有占位 `README.txt`：",
            "> " + ", ".join(f"`{n}`" for n in placeholders) + "。",
            "> 真实镜像需在 `FULL_BUILD=true` 下完成内核 / rootfs / UEFI 编译后替换，",
            "> 详见 `port/docs/PORTING-MAP.md` 第 4、6 节。",
            "",
        ]
    lines += ["## 校验", "", "```bash", "sha256sum -c SHA256SUMS", "```", ""]
    if policy["notes"]["include_checksums"]:
        lines += ["<details><summary>SHA256SUMS 全文</summary>", "", "```",
                  sums_text.rstrip(), "```", "", "</details>", ""]
    lines += ["## 刷机安全提示", ""]
    lines += [f"- {s}" for s in policy["notes"]["safety_reminder"]]
    lines += ["", "## 分区与双系统", "",
              "- 分区：`sunuefi_esp` + `sunuefi_root`（标签 `PIANOROOT`），**不动 `userdata`**。",
              "- 切换：UEFI 启动选择器 + `piano-boot-request`（BootRequest 语义）。",
              "- 账号：`piano` / `piano`，root 密码 `piano`。", ""]
    return "\n".join(lines)


def main() -> int:
    default_policy = Path(__file__).resolve().parent.parent / "config/release-policy.json"
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artifacts", type=Path, required=True, help="download-artifact 落地根目录")
    ap.add_argument("--out", type=Path, required=True, help="发布资产输出目录")
    ap.add_argument("--tag", required=True, help="Release tag")
    ap.add_argument("--policy", type=Path, default=default_policy)
    ap.add_argument("--repo", default="", help="owner/repo，用于生成 run 链接与说明")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--commit", default="")
    ap.add_argument("--upstream-baseline", default="", help="上游同步基线描述（可选）")
    ap.add_argument("--allow-partial", action="store_true", help="放宽必需产物校验（仅自检用）")
    ap.add_argument("--force", action="store_true", help="输出目录非空时仍覆盖")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON 摘要")
    a = ap.parse_args()

    src = a.artifacts.resolve()
    if not src.is_dir():
        raise SystemExit(f"产物目录不存在: {src}")
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()) and not a.force:
        raise SystemExit(f"输出目录非空，需 --force: {out}")
    out.mkdir(parents=True, exist_ok=True)

    policy = json.loads(a.policy.read_text())
    dirs = find_artifact_dirs(src)
    names = [d.name for d in dirs]
    missing = []
    for req in policy["required_artifacts"]:
        if not any(n == req or (req.endswith("-") and n.startswith(req)) for n in names):
            missing.append(req)
    if missing and not a.allow_partial:
        raise SystemExit("发布被拒绝，缺少必需产物: " + ", ".join(missing)
                         + "（download-artifact 是否用了 pattern: SunUEFI-* ？）")
    if not dirs:
        raise SystemExit(f"未发现任何 SunUEFI-* 产物目录: {src}")

    # 0) 上游基线信息（离线可读，来自 port/config/upstream-sync.json）
    upstream = {"url": "(未知)", "branch": "(未知)", "baseline": a.upstream_baseline}
    ucfg = a.policy.resolve().parent / "upstream-sync.json"
    if ucfg.is_file():
        u = json.loads(ucfg.read_text()).get("upstream", {})
        upstream["url"] = u.get("url", upstream["url"])
        upstream["branch"] = u.get("branch", upstream["branch"])

    artifacts: dict = {}
    for d in dirs:
        zip_path = out / f"{d.name}.zip"
        inner = make_zip(d, zip_path)
        artifacts[d.name] = {
            "zip": zip_path.name,
            "bytes": zip_path.stat().st_size,
            "sha256": sha256(zip_path),
            "source_dir": d.name,
            "files": inner,
            "status": artifact_status(inner),
        }

    placeholder_count = sum(1 for r in artifacts.values() if r["status"] == "PLACEHOLDER")
    status = ("RELEASE_ASSETS_CONTAINS_PLACEHOLDERS" if placeholder_count
              else "RELEASE_ASSETS_READY")
    meta = {
        "tag": a.tag,
        "release_name": policy["release_name_template"].replace("{tag}", a.tag),
        "repo": a.repo, "run_id": a.run_id, "commit": a.commit,
        "built_at_utc": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "upstream": upstream,
        "status": status,
        "policy": {"draft": policy["draft"], "prerelease": policy["prerelease"],
                   "clobber": policy["clobber"]},
    }

    manifest = {
        "schema_version": 1,
        "status": status,
        "tag": meta["tag"],
        "release_name": meta["release_name"],
        "repo": meta["repo"],
        "run_id": meta["run_id"],
        "commit": meta["commit"],
        "built_at_utc": meta["built_at_utc"],
        "upstream": upstream,
        "policy": meta["policy"],
        "artifacts": artifacts,
        "placeholder_count": placeholder_count,
        "warnings": (["存在占位产物，本 Release 仅验证 CI 链路，不可用于刷机"]
                     if placeholder_count else []),
        "note": "由 port/tools/port-release-assets.py 生成；仅写普通文件，不联网、不碰设备。",
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    # 1) 全局 SHA256SUMS：所有 zip + manifest.json（不含自身、不含 notes）
    sum_targets = sorted([out / r["zip"] for r in artifacts.values()]) + [manifest_path]
    sums_text = "".join(f"{sha256(p)}  {p.name}\n" for p in sum_targets)
    (out / "SHA256SUMS").write_text(sums_text)

    # 2) 发布说明
    (out / "RELEASE-NOTES.md").write_text(build_notes(meta, artifacts, policy, sums_text),
                                          encoding="utf-8")

    # 3) 待上传文件清单（每行一个文件名，不含本清单自身）
    upload = [r["zip"] for r in artifacts.values()] + \
             ["manifest.json", "SHA256SUMS", "RELEASE-NOTES.md"]
    (out / "upload-files.txt").write_text("\n".join(upload) + "\n")

    summary = {
        "status": status,
        "tag": meta["tag"],
        "release_name": meta["release_name"],
        "out": str(out),
        "artifacts": {n: {"zip": r["zip"], "bytes": r["bytes"],
                          "sha256": r["sha256"], "status": r["status"]}
                      for n, r in artifacts.items()},
        "placeholder_count": placeholder_count,
        "upload_files": upload,
    }
    if a.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\n[OK] Release 资产已生成于 {out}（{len(upload)} 个待上传文件，"
              f"占位产物 {placeholder_count} 个）。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
