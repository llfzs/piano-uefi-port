#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# 需求 3：随时同步 xiaomipad-8pro-mainline 原仓库，且永不与 UEFI 移植层冲突。
#
# 模型：本 fork = 上游原样目录 + 两个「port 自有命名空间」(port/ 与
#       .github/workflows/sunuefi-*.yml)。同步只把 upstream/main 合进上游受管
#       目录，port/ 与 workflow 是本 fork 独有文件，git 三方合并会自动保留。
#
# 用法：
#   ./port-sync-upstream.sh <fork 工作树> --check     # 只检查有无更新 + 隔离，不合并
#   ./port-sync-upstream.sh <fork 工作树> --merge     # 配置 remote、fetch、合并、校验
#   ./port-sync-upstream.sh <fork 工作树> --init      # 只配置 upstream remote
#
# 安全：fetch/merge 仅作用于本地 git；绝不 push；绝不碰设备；合并前做冲突预检，
#       若上游竟触及 port/ 或 sunuefi-*.yml 命名空间则立即中止并报告。
set -euo pipefail

ROOT="${1:-}"
ACTION="${2:---check}"
[ -n "$ROOT" ] && [ -d "$ROOT/.git" ] || {
  echo "用法: $0 <fork 工作树根> --init|--check|--merge" >&2; exit 2; }
ROOT="$(cd "$ROOT" && pwd)"
cd "$ROOT"

SYNC_CFG="port/config/upstream-sync.json"
[ -f "$SYNC_CFG" ] || { echo "缺少 $SYNC_CFG，请先运行 apply-port.sh" >&2; exit 2; }

REMOTE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream"]["name"])' "$SYNC_CFG")"
URL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream"]["url"])' "$SYNC_CFG")"
BRANCH="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["upstream"]["branch"])' "$SYNC_CFG")"

info() { printf '\033[36m[sync]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

init_remote() {
  if git remote get-url "$REMOTE" >/dev/null 2>&1; then
    info "remote '$REMOTE' 已存在: $(git remote get-url "$REMOTE")"
  else
    git remote add "$REMOTE" "$URL"
    info "已添加 remote '$REMOTE' -> $URL"
  fi
}

check_preconditions() {
  # 工作树必须干净，避免合并把本地未提交改动卷进去
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    die "工作树有未提交改动，先提交或 stash 再同步"
  fi
  info "工作树干净"
  # 隔离预检（合并前也跑一遍，确保当前状态本来就合规）
  python3 port/tools/port-check-isolation.py --root "$ROOT" --mode tree --json >/dev/null \
    || die "当前隔离校验未通过（port/ 结构异常），拒绝同步"
}

precheck_conflicts() {
  # 上游本次相对 merge-base 的改动，是否触碰了本 fork 自有命名空间
  local base
  base="$(git merge-base HEAD "$REMOTE/$BRANCH" 2>/dev/null || echo '')"
  [ -n "$base" ] || { warn "无法确定 merge-base（首次同步），跳过命名空间冲突预检"; return 0; }
  local touching
  touching="$(git diff --name-only "$base" "$REMOTE/$BRANCH" -- \
              'port/***' '.github/workflows/sunuefi-*' 2>/dev/null || true)"
  if [ -n "$touching" ]; then
    die "上游改动触及 port 自有命名空间，需人工处理，绝不自动覆盖：$touching"
  fi
  info "命名空间冲突预检通过：上游未触及 port/ 或 sunuefi-*.yml"
}

case "$ACTION" in
  --init)
    init_remote
    info "完成。可用 --check 查看更新、--merge 合并。"
    ;;
  --check)
    init_remote
    check_preconditions
    info "fetch $REMOTE/$BRANCH ..."
    git fetch --no-tags "$REMOTE" "$BRANCH" >/dev/null 2>&1 || die "fetch 失败（网络？）"
    local_sha="$(git rev-parse HEAD)"
    up_sha="$(git rev-parse "$REMOTE/$BRANCH")"
    behind="$(git rev-list --count HEAD.."$REMOTE/$BRANCH" 2>/dev/null || echo '?')"
    ahead="$(git rev-list --count "$REMOTE/$BRANCH"..HEAD 2>/dev/null || echo '?')"
    echo "  HEAD            = ${local_sha:0:12}"
    echo "  upstream/main   = ${up_sha:0:12}"
    echo "  落后(可同步)    = $behind 提交；本 fork 独有 = $ahead 提交"
    if [ "$local_sha" = "$up_sha" ]; then
      info "已是最新，无需同步"
    else
      info "有可用更新，合并请执行: $0 $ROOT --merge"
    fi
    ;;
  --merge)
    init_remote
    check_preconditions
    info "fetch $REMOTE/$BRANCH ..."
    git fetch --no-tags "$REMOTE" "$BRANCH" >/dev/null 2>&1 || die "fetch 失败"
    precheck_conflicts
    info "合并 $REMOTE/$BRANCH（只改上游受管目录，port/ 原样保留）..."
    if ! git merge --no-edit "$REMOTE/$BRANCH" 2>&1; then
      warn "合并报告冲突（多为上游改了与 port/ 无关文件的双方改动）。"
      warn "已中止合并，请人工 git status 处理；不要删除 port/ 或 sunuefi-*.yml。"
      git merge --abort 2>/dev/null || true
      die "合并未自动完成"
    fi
    info "合并完成，运行隔离校验 + 离线自检..."
    python3 port/tools/port-check-isolation.py --root "$ROOT" --mode git --json \
      || die "合并后隔离校验失败（不该发生）"
    if [ -x port/verify-port.sh ]; then
      bash port/verify-port.sh "$ROOT" >/dev/null 2>&1 && info "离线自检通过" \
        || warn "离线自检未通过，请手动运行 port/verify-port.sh"
    fi
    info "同步完成。推送本 fork 后将由 .github/workflows/sunuefi-build.yml 自动重建三产物。"
    ;;
  *)
    die "未知动作 $ACTION（用 --init/--check/--merge）"
    ;;
esac
