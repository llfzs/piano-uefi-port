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
    SunUEFI-Root-<target>--*.part0..N   大镜像超过上限时按字节分卷（不打单个超大 ZIP）
    manifest.json                 汇总清单（每个资产的 sha256、源文件、状态、构建元数据）
    SHA256SUMS                    全局校验和（针对所有发布资产 + manifest.json）
    RELEASE-NOTES.md              人类可读发布说明（含校验和、还原命令、上游基线、安全提示）
    upload-files.txt              每行一个待上传文件名，供 gh release create/upload 消费
    release-flags.env             发布门禁标记（CI 中间产物，不上传、不计入 SHA256SUMS）

设计约束：
  * 小产物 ZIP 用 STORED 模式 + 固定时间戳，避免数 GB 镜像压缩耗时，且产物可复现；
    流式写入，不把整个镜像读进内存。
  * GitHub Release 单资产上限 2 GiB：目录内任一文件 > 1,900,000,000 B（与上游
    assets-manifest.json 的 part_bytes 一致）时，改为字节分卷 .partN + 小文件平铺，
    RELEASE-NOTES.md 给出 `cat *.part* > 原文件名` 还原命令；--max-asset-bytes 可调阈值。
  * 严格校验 Module / ESP / 至少一个 Root 齐全（--allow-partial 仅供离线自检）。
  * 发布门禁：检测到占位产物时输出 release-flags.env（FORCE_DRAFT/FORCE_PRERELEASE），
    由 publish job source 后强制降级为草稿+预发布，避免骨架被当成正式版。
  * 只写 --out 目录，不碰上游受管目录、不联网、不调用 gh/fastboot。
"""
import argparse
import datetime
import hashlib
import json
import shlex
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


# GitHub Release 单资产上限 2 GiB；与上游 assets-manifest.json 的 part_bytes 对齐。
# 超过则按字节切分为 .partN（不压缩，避免数 GB 镜像的压缩耗时与额外内存）。
MAX_ASSET_BYTES = 1_900_000_000


def split_large_file(src: Path, dst_stem: Path, limit: int = MAX_ASSET_BYTES):
    """流式切分单个大文件为 .partN，返回 (parts, 整体 sha256, 总字节数)。"""
    h = hashlib.sha256()
    parts: list[Path] = []
    total = 0
    with src.open("rb") as f:
        idx = 0
        while True:
            part = dst_stem.with_name(dst_stem.name + f".part{idx}")
            written = 0
            with part.open("wb") as o:
                while written < limit:
                    chunk = f.read(min(4 * 1024 * 1024, limit - written))
                    if not chunk:
                        break
                    o.write(chunk)
                    h.update(chunk)
                    written += len(chunk)
            if written == 0:
                part.unlink(missing_ok=True)
                break
            total += written
            parts.append(part)
            idx += 1
            if written < limit:
                break
    return parts, h.hexdigest(), total


def make_parted_assets(d: Path, out: Path, limit: int = MAX_ASSET_BYTES):
    """超大产物：不生成单个 ZIP（会超 2 GiB 上限），直接把目录内文件平铺/切分。

    返回 (asset_paths, asset_rows, inner_names, restore_text)。
    limit 可由 --max-asset-bytes 调小，便于离线自检覆盖分卷路径。
    """
    files = collect_files(d)
    assets: list[Path] = []
    rows: list[dict] = []
    restore: list[str] = []
    for p in files:
        rel = p.relative_to(d).as_posix()
        stem = out / f"{d.name}--{rel.replace('/', '_')}"
        if p.stat().st_size > limit:
            parts, digest, _ = split_large_file(p, stem, limit)
            for q in parts:
                assets.append(q)
                rows.append({"name": q.name, "bytes": q.stat().st_size,
                             "sha256": sha256(q)})
            restore.append(f"cat {stem.name}.part* > {Path(rel).name}"
                           f"   # 还原后 sha256={digest}")
        else:
            shutil.copyfile(p, stem)
            assets.append(stem)
            rows.append({"name": stem.name, "bytes": stem.stat().st_size,
                         "sha256": sha256(stem)})
    return assets, rows, [p.relative_to(d).as_posix() for p in files], "\n".join(restore)


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
        "小体积产物压成同名 ZIP（内含目录级 `SHA256SUMS`）；超过 Release 单文件 2 GiB "
        "上限的大镜像改为字节分卷 `.part*`，按下方命令还原。",
        "",
        "| 文件 | 大小 | SHA256 | 状态 |",
        "|---|---|---|---|",
    ]
    for name, row in artifacts.items():
        for ar in row["asset_rows"]:
            lines.append(f"| `{ar['name']}` | {human(ar['bytes'])} | "
                         f"`{ar['sha256']}` | {row['status']} |")
    lines += [
        "",
        "| 附加文件 | 说明 |",
        "|---|---|",
        "| `manifest.json` | 汇总清单（各产物源文件、状态、校验和、构建元数据） |",
        "| `SHA256SUMS` | 全局校验和，覆盖所有发布资产与 `manifest.json` |",
        "| `RELEASE-NOTES.md` | 本文件 |",
        "",
    ]
    restores = [(n, r["restore"]) for n, r in artifacts.items() if r.get("restore")]
    if restores:
        lines += [
            "## 还原分卷镜像",
            "",
            "大镜像按 1,900,000,000 字节切分（与上游 `assets-manifest.json` 的 "
            "`part_bytes` 一致，且低于 GitHub Release 单资产 2 GiB 上限）。",
            "下载同一组 `.part*` 全部文件后，在空目录执行：",
            "",
            "```bash",
        ]
        for n, txt in restores:
            lines += [f"# {n}", txt]
        lines += ["```", ""]
    placeholders = [n for n, r in artifacts.items() if r["status"] == "PLACEHOLDER"]
    if placeholders and policy["notes"]["include_placeholder_warning"]:
        lines += [
            "> ⚠️ **这是 CI 骨架产物，不是可刷入的固件。** 以下产物目录里只有占位 `README.txt`：",
            "> " + ", ".join(f"`{n}`" for n in placeholders) + "。",
            "> 真实镜像需在 `FULL_BUILD=true` 下完成内核 / rootfs / UEFI 编译后替换，",
            "> 详见 `port/docs/PORTING-MAP.md` 第 4、6 节与 `port/docs/RELEASE.md`。",
            "",
        ]
    guard = meta.get("publish_guard") or {}
    if guard.get("status") == "GUARD_TRIGGERED":
        lines += [
            "> 🛑 **发布门禁已生效**：检测到占位产物，本次 Release 被强制创建为",
            "> **草稿 (draft) + 预发布 (prerelease)**，不会作为正式版本发布。",
            "> " + guard.get("message", ""),
            "> 门禁标记见同目录 `release-flags.env`（CI 中间文件，不上传）。",
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
    ap.add_argument("--max-asset-bytes", type=int, default=MAX_ASSET_BYTES,
                    help="单个 Release 资产字节上限，超过则分卷（默认 1900000000）")
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
        limit = a.max_asset_bytes
        files = collect_files(d)
        oversized = any(p.stat().st_size > limit for p in files)
        if oversized:
            # 单个 ZIP 会超过 Release 单资产上限：改为字节分卷 + 平铺小文件
            _assets, rows, inner, restore = make_parted_assets(d, out, limit)
            artifacts[d.name] = {
                "zip": f"{d.name}.zip",   # 逻辑产物名（兼容说明/manifest），实际按 assets 上传
                "assets": [r["name"] for r in rows],
                "asset_rows": rows,
                "bytes": sum(r["bytes"] for r in rows),
                "sha256": "",             # 分卷后无单一文件；各 part 校验和见 asset_rows
                "parted": True,
                "restore": restore,
                "source_dir": d.name,
                "files": inner,
                "status": artifact_status(inner),
            }
        else:
            zip_path = out / f"{d.name}.zip"
            inner = make_zip(d, zip_path)
            zsha = sha256(zip_path)
            zbytes = zip_path.stat().st_size
            artifacts[d.name] = {
                "zip": zip_path.name,
                "assets": [zip_path.name],
                "asset_rows": [{"name": zip_path.name, "bytes": zbytes, "sha256": zsha}],
                "bytes": zbytes,
                "sha256": zsha,
                "parted": False,
                "restore": "",
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

    # ---- 发布门禁（需求 7 补强）：占位产物不得作为正式版发布 ----
    guard_cfg = policy.get("placeholder_guard") or {}
    guard_enabled = bool(guard_cfg.get("enabled"))
    triggered = (guard_enabled and placeholder_count > 0
                 and guard_cfg.get("on_placeholder") == "draft_prerelease")
    guard_status = ("GUARD_TRIGGERED" if triggered
                    else ("GUARD_CLEAR" if guard_enabled else "GUARD_DISABLED"))
    guard_message = guard_cfg.get("message", "") if triggered else ""
    publish_guard = {
        "enabled": guard_enabled,
        "status": guard_status,
        "placeholder_count": placeholder_count,
        "force_draft": triggered,
        "force_prerelease": triggered,
        "allow_override_input": guard_cfg.get("allow_override_input", ""),
        "message": guard_message,
    }
    # CI 中间文件：可 source 的 shell 变量，publish job 据此覆盖 --draft/--prerelease。
    # 故意不加入 upload-files.txt 与全局 SHA256SUMS。
    (out / guard_cfg.get("flags_file", "release-flags.env")).write_text(
        f"FORCE_DRAFT={int(triggered)}\n"
        f"FORCE_PRERELEASE={int(triggered)}\n"
        f"GUARD_STATUS={guard_status}\n"
        f"GUARD_MESSAGE={shlex.quote(guard_message)}\n",
        encoding="utf-8")
    meta["publish_guard"] = publish_guard

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
        "publish_guard": publish_guard,
        "warnings": (["存在占位产物，本 Release 仅验证 CI 链路，不可用于刷机"]
                     + (["发布门禁已生效：强制草稿 + 预发布"] if triggered else [])
                     if placeholder_count else []),
        "note": "由 port/tools/port-release-assets.py 生成；仅写普通文件，不联网、不碰设备。",
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    # 1) 全局 SHA256SUMS：所有 zip + manifest.json（不含自身、不含 notes）
    asset_names = sorted(n for r in artifacts.values() for n in r["assets"])
    sum_targets = [out / n for n in asset_names] + [manifest_path]
    sums_text = "".join(f"{sha256(p)}  {p.name}\n" for p in sum_targets)
    (out / "SHA256SUMS").write_text(sums_text)

    # 2) 发布说明
    (out / "RELEASE-NOTES.md").write_text(build_notes(meta, artifacts, policy, sums_text),
                                          encoding="utf-8")

    # 3) 待上传文件清单（每行一个文件名，不含本清单自身）
    upload = asset_names + ["manifest.json", "SHA256SUMS", "RELEASE-NOTES.md"]
    # 安全网：任何待上传资产都不得达到 GitHub Release 单文件 2 GiB 上限
    too_big = [n for n in asset_names
               if (out / n).stat().st_size >= 2 * 1024 * 1024 * 1024]
    if too_big:
        raise SystemExit("发布资产超过 2 GiB 上限，分卷逻辑异常: " + ", ".join(too_big))
    (out / "upload-files.txt").write_text("\n".join(upload) + "\n")

    summary = {
        "status": status,
        "tag": meta["tag"],
        "release_name": meta["release_name"],
        "out": str(out),
        "artifacts": {n: {"zip": r["zip"], "bytes": r["bytes"],
                          "sha256": r["sha256"], "status": r["status"],
                          "assets": r["assets"], "parted": r["parted"],
                          "restore": r["restore"]}
                      for n, r in artifacts.items()},
        "placeholder_count": placeholder_count,
        "publish_guard": publish_guard,
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
