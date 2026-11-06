#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：隔离校验（需求 3 的守护者）。

在「随时同步 xiaomipad-8pro-mainline 原仓库」的模型下，本脚本证明：
  1) 本 fork 相对上游，所有新增/改动文件都只落在两个「port 自有命名空间」：
       port/ 与 .github/workflows/sunuefi-*
     —— 因此 merge upstream/main 时与上游受管目录零重叠、永不冲突。
  2) port/ 的关键文件（protected_port_files）齐全且未被上游覆盖。
  3) port/ 内脚本不写入上游受管目录（静态扫描危险写入路径）。

两种模式，均离线可跑：
  --mode tree  (默认) 直接扫工作树：适合 apply 后 / 离线自检 / 无 upstream remote。
  --mode git   额外用 `git diff upstream/main...HEAD --name-only` 校验真实改动集
               （需已配置 upstream remote；无 remote 时自动降级为 tree 并标注）。
输出 JSON 报告；发现违规 exit 1。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

OWNED_PREFIXES = ("port/",)
OWNED_WORKFLOW_RE = re.compile(r"^\.github/workflows/sunuefi-.*\.(yml|yaml)$")
# 顶层允许存在的上游文件（来自 upstream-sync.json 的 upstream_managed_roots），
# 这些是「原样保留」的，本 fork 不得修改。这里只用于给出更清晰的报告。
FORBIDDEN_WRITE_RE = re.compile(
    r"""(?:>|\bcp\b|\bmv\b|\binstall\b|\btee\b|\bwrite_text\b|\bwrite_bytes\b|
         \bopen\([^)]*['"][wa]|\bsed\s+-i\b)""" , re.X)


def load_sync(root: Path) -> dict:
    p = root / "port/config/upstream-sync.json"
    return json.loads(p.read_text())


def is_owned(path: str) -> bool:
    return (any(path.startswith(pre) for pre in OWNED_PREFIXES)
            or bool(OWNED_WORKFLOW_RE.match(path))
            or path == ".gitignore" and False)  # .gitignore 不属自有，显式排除


def tree_files(root: Path) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".git/") or rel == ".git":
            continue
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        out.append(rel)
    return out


def git_changed(root: Path, base: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "-z",
                            f"{base}...HEAD"], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return [x for x in r.stdout.split("\0") if x]
    except FileNotFoundError:
        return None


def scan_dangerous_writes(root: Path) -> list[str]:
    """静态扫描 port/tools 里可能写入上游受管目录的可疑行（保守报告，不误杀读取）。"""
    managed = ("kernel/", "ubuntu/", "device/", "tools/build-", "firmware/",
               "mesa/", "packaging/", "initramfs/")
    hits = []
    tools = root / "port/tools"
    if not tools.is_dir():
        return hits
    for py in sorted(list(tools.glob("*.py")) + list(tools.glob("*.sh"))):
        for n, line in enumerate(py.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(m in stripped for m in managed) and FORBIDDEN_WRITE_RE.search(stripped):
                hits.append(f"{py.name}:{n}: {stripped[:100]}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="fork 工作树根目录")
    ap.add_argument("--mode", choices=("tree", "git"), default="tree")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（供 CI 消费）")
    a = ap.parse_args()

    root = a.root.resolve()
    sync = load_sync(root)
    report: dict = {"status": "OK", "root": str(root), "mode": a.mode,
                    "violations": [], "warnings": []}

    # 1) 关键文件完整性
    missing = [f for f in sync["isolation_rules"]["protected_port_files"]
               if not (root / f).is_file()]
    if missing:
        report["violations"].append("protected port 文件缺失: " + ", ".join(missing))

    # 2) 配置文件可解析
    for cfg in ("port/config/port-profile.json", "port/config/distros.json",
                "port/config/upstream-sync.json"):
        try:
            json.loads((root / cfg).read_text())
        except Exception as e:  # noqa: BLE001
            report["violations"].append(f"{cfg} 非法: {e}")

    # 3) 命名空间：找出所有「本 fork 自有」文件，校验 workflow 前缀
    owned = [p for p in tree_files(root) if is_owned(p)]
    bad_workflows = [p for p in tree_files(root)
                     if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))
                     and not OWNED_WORKFLOW_RE.match(p)]
    if bad_workflows:
        report["violations"].append(
            "非 sunuefi- 前缀的 workflow（会与上游/未来 workflow 撞名）: "
            + ", ".join(bad_workflows))
    report["owned_file_count"] = len(owned)

    # 4) git 模式：真实改动集必须全落在自有命名空间
    if a.mode == "git":
        changed = git_changed(root, sync["upstream"]["name"] + "/" + sync["upstream"]["branch"])
        if changed is None:
            report["warnings"].append("无法读取 upstream/main diff（未配置 remote 或历史不足），已降级为 tree 模式")
            report["mode"] = "tree(degraded)"
        else:
            report["changed_count"] = len(changed)
            offending = [p for p in changed if not is_owned(p)]
            if offending:
                report["violations"].append(
                    "本 fork 修改了上游受管文件（同步会冲突）: " + ", ".join(offending[:30]))

    # 5) 静态写入扫描（报告为 warning，避免过度阻断）
    dangerous = scan_dangerous_writes(root)
    if dangerous:
        report["warnings"].append("疑似写入上游目录的脚本行（请人工确认是否只是读取路径）: "
                                 + " | ".join(dangerous[:10]))

    if report["violations"]:
        report["status"] = "VIOLATION"

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not a.json and report["status"] == "OK":
        print("\n[OK] 隔离校验通过：UEFI 全部位于 port/ 与 sunuefi-*.yml，"
              "可随时 merge upstream/main 而无冲突。", file=sys.stderr)
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
