#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# 上游 Release 监视（独立 port 仓库模型）。
#
# 本仓库不含 yzddmr6/xiaomipad-8pro-mainline 的源码副本，不做 git merge。
# 「跟随上游」= 监视上游按 tag 发布的编译成品，发现新 tag 就（可选）派发重建。
# 与 .github/workflows/sunuefi-sync-upstream.yml 的逻辑一致，本脚本供本地使用。
#
# 用法：
#   ./port-sync-upstream.sh <仓库根> --check                # 只检查上游最新 tag + 去重
#   ./port-sync-upstream.sh <仓库根> --dispatch             # 发现新 tag 时派发 repository_dispatch
#   ./port-sync-upstream.sh <仓库根> --tag v1.0.0 --check   # 指定 tag
#
# 需要环境变量 GH_TOKEN（有 contents:write 的 token）用于 API 查询/派发。
# 安全：只读上游 API；--dispatch 也只向本仓库发事件，不 push、不碰设备。
set -euo pipefail

ROOT="${1:-}"
ACTION="--check"
WANT_TAG=""; REPO="${GITHUB_REPOSITORY:-llfzs/piano-uefi-port}"
UPSTREAM_REPO="yzddmr6/xiaomipad-8pro-mainline"

[ $# -gt 0 ] && shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --check|--dispatch) ACTION="$1"; shift;;
    --tag) WANT_TAG="${2:-}"; shift 2;;
    --repo) REPO="${2:-}"; shift 2;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done

[ -n "$ROOT" ] && [ -d "$ROOT/port" ] || {
  echo "用法: $0 <本仓库根（含 port/）> --check|--dispatch [--tag <tag>] [--repo <owner/repo>]" >&2
  exit 2; }
ROOT="$(cd "$ROOT" && pwd)"
[ -f "$ROOT/port/config/upstream-sync.json" ] || {
  echo "缺 port/config/upstream-sync.json" >&2; exit 2; }

: "${GH_TOKEN:?需要 GH_TOKEN 环境变量（contents:write）}"

info() { printf '\033[36m[watch]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

api() {
  local method="${1:-GET}" url="$2" body="${3:-}"
  local -a args=(-sSf -X "$method" -H "Authorization: Bearer $GH_TOKEN" \
                 -H "Accept: application/vnd.github+json" \
                 -H "X-GitHub-Api-Version: 2022-11-28")
  if [ -n "$body" ]; then
    args+=(-H "Content-Type: application/json" -d "$body")
  fi
  curl "${args[@]}" "$url"
}

if [ -n "$WANT_TAG" ]; then
  TAG="$WANT_TAG"
else
  TAG="$(api GET "https://api.github.com/repos/$UPSTREAM_REPO/releases/latest" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin).get("tag_name",""))')"
fi
[ -n "$TAG" ] && [ "$TAG" != "null" ] || die "无法取得上游 Release tag"
info "上游 Release tag = $TAG"

NEEDLE="$UPSTREAM_REPO@$TAG"
ALREADY="$(api GET "https://api.github.com/repos/$REPO/releases?per_page=30" \
          | python3 -c "import json,sys;print(len([r for r in json.load(sys.stdin) if '$NEEDLE' in (r.get('body') or '')]))" \
          2>/dev/null || echo 0)"
if [ "$ALREADY" != "0" ]; then
  info "本仓库已有对应 $TAG 的 Release，无需重建"
  exit 0
fi
info "本仓库尚无对应 $TAG 的构建"

if [ "$ACTION" = "--dispatch" ]; then
  api POST "https://api.github.com/repos/$REPO/dispatches" \
      "{\"event_type\":\"upstream-release\",\"client_payload\":{\"upstream_tag\":\"$TAG\",\"source\":\"upstream-release\"}}" \
      >/dev/null
  info "已派发 upstream-release 事件 -> sunuefi-build.yml 将拉新成品重建"
else
  info "（--check）如需重建请执行: $0 $ROOT --dispatch --tag $TAG"
fi
