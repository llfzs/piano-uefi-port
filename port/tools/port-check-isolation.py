#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""桥接层：port 层自包含校验（独立仓库模型）。

本仓库是 **独立的 UEFI 移植层仓库**，不含 yzddmr6/xiaomipad-8pro-mainline 的源码
副本，也不做 git merge upstream/main。上游源码由上游自己维护，本仓库只消费他按 tag
发布的编译成品（见 port/build/port-from-upstream-release.sh）。

因此本脚本校验的是「port 层自包含」：
  1) 所有必要文件都在 port/ 内、配置可解析、protected_port_files 齐全；
  2) workflow 全部带 sunuefi- 前缀，避免与本仓库未来的 workflow 撞名；
  3) 仓库顶层不得混入上游源码目录（kernel/、device/、ubuntu/ ...）——
     独立仓库若出现这些目录，说明误把上游源码塞进来了，会破坏「只消费成品」的模型；
  4) port/ 内脚本不往仓库根的上游目录写文件（静态扫描，保守报告为 warning）。

两种模式，均离线可跑：
  --mode tree  (默认) 直接扫工作树：适合 CI、离线自检。
  --mode git   额外用 `git diff HEAD~1..HEAD --name-only` 校验本次改动集都落在
               port/ 自有命名空间（本仓库自身历史，不依赖 upstream remote）。
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
# 独立仓库允许存在的根级文件（不属于上游源码）
ALLOWED_ROOT_FILES = {
    ".gitignore", "README.md", "README.en.md", "LICENSE", "NOTICE",
    "REUSE.toml", "AGENTS.md", "CONTRIBUTING.md", ".gitattributes",
}
# 上游源码目录：独立仓库中不应出现（出现即说明误把上游源码副本塞进来了）
UPSTREAM_SOURCE_DIRS = (
    "apps/", "device/", "docs/", "firmware/", "initramfs/", "kernel/",
    "mesa/", "packaging/", "ubuntu/", "tools/",
)
FORBIDDEN_WRITE_RE = re.compile(
    r"""(?:>|\bcp\b|\bmv\b|\binstall\b|\btee\b|\bwrite_text\b|\bwrite_bytes\b|
         \bopen\([^)]*['"][wa]|\bsed\s+-i\b)""", re.X)


def load_sync(root: Path) -> dict:
    p = root / "port/config/upstream-sync.json"
    return json.loads(p.read_text())


def is_owned(path: str) -> bool:
    return (any(path.startswith(pre) for pre in OWNED_PREFIXES)
            or bool(OWNED_WORKFLOW_RE.match(path))
            or path in ALLOWED_ROOT_FILES)


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


def git_changed(root: Path) -> list[str] | None:
    """本仓库自身最近一次提交的改动集（不依赖 upstream remote）。"""
    try:
        r = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "-z",
                            "HEAD~1..HEAD"], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return [x for x in r.stdout.split("\0") if x]
    except FileNotFoundError:
        return None


def scan_dangerous_writes(root: Path) -> list[str]:
    """静态扫描 port/tools 里可能写入仓库根上游目录的可疑行（保守报告，不误杀读取）。"""
    hits = []
    tools = root / "port/tools"
    if not tools.is_dir():
        return hits
    for py in sorted(list(tools.glob("*.py")) + list(tools.glob("*.sh"))):
        for n, line in enumerate(py.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(m in stripped for m in UPSTREAM_SOURCE_DIRS) and FORBIDDEN_WRITE_RE.search(stripped):
                hits.append(f"{py.name}:{n}: {stripped[:100]}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="独立 port 仓库根目录")
    ap.add_argument("--mode", choices=("tree", "git"), default="tree")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（供 CI 消费）")
    a = ap.parse_args()

    root = a.root.resolve()
    sync = load_sync(root)
    report: dict = {"status": "OK", "root": str(root), "mode": a.mode,
                    "model": "standalone-port-repo", "violations": [], "warnings": []}

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

    all_files = tree_files(root)
    owned = [p for p in all_files if is_owned(p)]

    # 3) 独立仓库不得混入上游源码目录
    smuggled = sorted({p.split("/")[0] + "/" for p in all_files
                       if any(p.startswith(d) for d in UPSTREAM_SOURCE_DIRS)})
    if smuggled:
        report["violations"].append(
            "独立 port 仓库顶层混入了上游源码目录（应只消费上游 Release 成品）: "
            + ", ".join(smuggled))
    report["owned_file_count"] = len(owned)

    # 4) workflow 前缀
    bad_workflows = [p for p in all_files
                     if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))
                     and not OWNED_WORKFLOW_RE.match(p)]
    if bad_workflows:
        report["violations"].append(
            "非 sunuefi- 前缀的 workflow（会撞名）: " + ", ".join(bad_workflows))

    # 5) git 模式：本仓库自身改动集必须全落在自有命名空间
    if a.mode == "git":
        changed = git_changed(root)
        if changed is None:
            report["warnings"].append("无法读取 HEAD~1..HEAD 改动集（历史不足），已跳过该检查")
        else:
            report["changed_count"] = len(changed)
            offending = [p for p in changed if not is_owned(p)]
            if offending:
                report["violations"].append(
                    "改动落在 port/ 自有命名空间之外: " + ", ".join(offending[:30]))

    # 6) 静态写入扫描（报告为 warning，避免过度阻断）
    dangerous = scan_dangerous_writes(root)
    if dangerous:
        report["warnings"].append("疑似写入仓库根上游目录的脚本行（请人工确认是否只是读取路径）: "
                                 + " | ".join(dangerous[:10]))

    if report["violations"]:
        report["status"] = "VIOLATION"

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not a.json and report["status"] == "OK":
        print("\n[OK] port 层自包含校验通过：无上游源码副本、workflow 前缀正确、"
              "关键文件齐全。上游成品由 CI 动态拉取。", file=sys.stderr)
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
