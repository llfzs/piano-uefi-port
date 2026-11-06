#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# 离线校验：port 层结构 + 脚本语法 + YAML + 合成输入自检 + 自包含校验。
# 不编译、不联网、不刷机。
# 用法（本仓库为独立 port 仓库，扁平布局）：
#   bash port/verify-port.sh               # 从仓库根校验自身
#   bash port/verify-port.sh .             # 同上，显式传仓库根
# 兼容旧工程包布局（含 overlay/ + apply-port.sh）的 KIT 模式，便于对照验证。
set -euo pipefail
SELF="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1" >&2; exit 1; }

# 判定模式：旧工程包 KIT 模式（存在 overlay/）还是独立仓库 SUBTREE 模式
KIT=""; TARGET=""
if [ -d "$SELF/overlay/port" ]; then
  KIT="$SELF"
  [ -n "${1:-}" ] && TARGET="$1"
else
  # 从 fork 内调用：SELF 可能是 <fork>/port，KIT 工具都在自身
  TARGET="$(cd "$SELF/.." && pwd)"
fi

# 定位 port 根与 tools
if [ -n "$KIT" ]; then
  PORTROOT="$KIT/overlay/port"
else
  PORTROOT="$(cd "$(dirname "$SELF")" && pwd)"   # 期望是 <fork>/port 的父
  [ -d "$SELF/tools" ] && PORTROOT="$SELF"
fi
[ -d "$PORTROOT/tools" ] || fail "找不到 port/tools（PORTROOT=$PORTROOT）"

echo "=== 1. 关键文件齐全 ==="
for f in \
  config/port-profile.json config/distros.json config/upstream-sync.json config/release-policy.json \
  tools/port-stage-esp.py tools/port-adapt-rootfs.py tools/port-partition-plan.py \
  tools/port-inject-login.py tools/port-package-artifacts.py tools/port-release-assets.py \
  tools/port-check-isolation.py tools/port-sync-upstream.sh tools/port-switch.sh \
  tools/port-upstream-release.py \
  build/port-build.sh build/port-full-build.sh build/port-from-upstream-release.sh \
  docs/PORTING-MAP.md docs/REQUIREMENTS.md docs/SYNC-UPSTREAM.md docs/RELEASE.md \
  docs/UPSTREAM-RELEASE.md; do
  [ -f "$PORTROOT/$f" ] || fail "缺文件 port/$f"
done
pass "port/ 关键文件齐全（含需求 3/4/5/7 脚本、真实编译接线器与文档）"

echo "=== 2. Python / Shell 语法 ==="
python3 - "$PORTROOT" <<'PY' || fail "python 语法错误"
import ast, sys
from pathlib import Path
root = Path(sys.argv[1])
files = list((root/'tools').glob('*.py'))
tests = root/'tests/selftest.py'
fixture = root/'tests/make_upstream_fixture.py'
if tests.exists(): files.append(tests)
if fixture.exists(): files.append(fixture)
for p in files:
    ast.parse(p.read_text(), filename=str(p))
print(f"  parsed {len(files)} python files")
PY
bash -n "$PORTROOT"/tools/*.sh "$PORTROOT"/build/*.sh 2>/dev/null || fail "shell 语法错误"
if [ -n "$KIT" ]; then
  bash -n "$KIT"/apply-port.sh "$KIT"/verify-port.sh || fail "顶层 shell 语法错误"
fi
pass "脚本语法合法"

echo "=== 3. JSON 配置合法 + 需求字段断言 ==="
python3 - "$PORTROOT" <<'PY' || fail "配置校验失败"
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
prof = json.loads((root/'config/port-profile.json').read_text())
dist = json.loads((root/'config/distros.json').read_text())
sync = json.loads((root/'config/upstream-sync.json').read_text())
rel = json.loads((root/'config/release-policy.json').read_text())
# 需求 2
assert prof['partitions']['esp']['name'] == 'sunuefi_esp'
assert prof['partitions']['root']['name'] == 'sunuefi_root'
assert prof['partitions']['root_label'] == 'PIANOROOT'
# 需求 5
assert prof['login']['user'] == 'piano'
assert prof['login']['password'] == 'piano'
assert prof['login']['root_password'] == 'piano'
assert prof['login']['autologin'] is True
# 需求 4
assert all(k in prof['artifacts'] for k in ('SunUEFI-Module','SunUEFI-ESP','SunUEFI-Root'))
assert any(d['enabled'] for d in dist['distros'])
# 需求 3
assert 'port/' in sync['port_owned_namespaces']
assert any('sunuefi-' in n for n in sync['port_owned_namespaces'])
# 需求 7：发布策略
assert rel['tag_prefix'] == 'v' and rel['tag_trigger_glob'] == 'v*'
assert rel['clobber'] is True
assert 'SunUEFI-Module.zip' in rel['upload_files']
assert any(x.startswith('SunUEFI-Root-') for x in rel['upload_files'])
assert len(rel['required_artifacts']) == 3
# 需求 7 补强：发布门禁
guard = rel['placeholder_guard']
assert guard['enabled'] is True
assert guard['on_placeholder'] == 'draft_prerelease'
assert guard['allow_override_input'] == 'allow_placeholder_release'
assert guard['flags_file'] == 'release-flags.env'
assert 'port/tools/port-release-assets.py' in sync['isolation_rules']['protected_port_files']
assert 'port/build/port-full-build.sh' in sync['isolation_rules']['protected_port_files']
# 需求 3 补强：上游 Release 适配器必须登记为受保护文件
protected = sync['isolation_rules']['protected_port_files']
assert 'port/tools/port-upstream-release.py' in protected, '缺上游 Release 适配器登记'
assert 'port/build/port-from-upstream-release.sh' in protected, '缺上游 Release 编排器登记'
print('  profile/distros/sync/release-policy 需求字段 OK（含发布门禁 + 上游 Release 接线）')
PY
pass "配置合法（分区/账号/产物/同步/发布字段）"

echo "=== 4. GitHub Actions YAML 结构 + 三产物 + 需求 7 发布链路 ==="
WF=""
if [ -n "$KIT" ]; then WF="$KIT/overlay/.github/workflows"; else WF="$(dirname "$PORTROOT")/.github/workflows"; fi
if [ -d "$WF" ]; then
  python3 - "$WF" <<'PY' || fail "workflow YAML 校验失败"
import sys, glob
from pathlib import Path
try:
    import yaml
except ImportError:
    yaml = None
wf = Path(sys.argv[1])
files = sorted(glob.glob(str(wf/'sunuefi-*.yml')))
assert files, "没有 sunuefi-*.yml"
names = [Path(f).name for f in files]
for f in files:
    name = Path(f).name
    assert name.startswith('sunuefi-'), f"workflow 缺前缀: {name}"
    text = Path(f).read_text()
    assert 'jobs:' in text, f"{name} 缺 jobs"
    if yaml:
        d = yaml.safe_load(text)
        assert isinstance(d, dict) and 'jobs' in d, f"{name} 缺 jobs"
# 需求 6：三个 artifact 名
build_text = (wf/'sunuefi-build.yml').read_text()
for token in ('name: SunUEFI-Module', 'name: SunUEFI-ESP', 'SunUEFI-Root-${{ matrix.target }}'):
    assert token in build_text, f"构建 workflow 缺 {token}"
# 需求 7：tag 触发 / publish job / 发布链路 / 权限
assert 'sunuefi-release.yml' in names, '缺手动补发 workflow'
for token in ('tags:', 'v*', 'publish_release:', 'publish:', 'port-release-assets.py',
              'pattern: SunUEFI-*', 'gh release', '--clobber', 'contents: write'):
    assert token in build_text, f"构建 workflow 缺需求 7 要素: {token}"
# 真实编译接线：不再允许 echo 占位 / 静默 || true
assert '示例接线' not in build_text, '仍残留「示例接线」echo 占位'
assert '应软链/复制到 build/port-inputs' not in build_text, '仍残留旧占位提示'
for token in ('port/build/port-full-build.sh', '--external-inputs',
              '--inputs "$FB_INPUTS"', '--esp-image', '--root-image', '--module-zip',
              'port-inputs.env', 'FULL_BUILD'):
    assert token in build_text, f"构建 workflow 缺真实接线要素: {token}"
# 需求 3 补强：默认输入源必须是上游最新 Release 成品（跟着上游更新）
for token in ('source:', 'upstream-release', 'upstream-release-meta',
              'port-from-upstream-release.sh', 'SHA256SUMS.assets',
              'SOURCE', 'UPSTREAM_TAG', 'client_payload.upstream_tag'):
    assert token in build_text, f"构建 workflow 缺上游 Release 接线要素: {token}"
assert 'tee build-port.log || true' not in build_text, '组装步骤仍用 || true 掩盖失败'
# 发布门禁：两个 workflow 都必须有
for token in ('allow_placeholder_release', 'release-flags.env', 'FORCE_DRAFT',
              '应用发布门禁', 'GUARD_STATUS'):
    assert token in build_text, f"构建 workflow 缺发布门禁要素: {token}"
if yaml:
    bd = yaml.safe_load(build_text)
    assert 'publish' in bd['jobs'], 'publish job 解析失败'
    assert bd['jobs']['publish']['permissions']['contents'] == 'write'
    assert bd['jobs']['publish']['needs'] == ['select', 'build']
    psteps = [s.get('name') for s in bd['jobs']['publish']['steps']]
    assert '应用发布门禁（占位骨架不得作为正式版）' in psteps
# 手动补发 workflow：复用已有 run、不重新编译，同样带门禁
rel_text = (wf/'sunuefi-release.yml').read_text()
for token in ('workflow_dispatch:', 'gh run download', 'port-release-assets.py',
              'allow_placeholder_release', 'release-flags.env', 'FORCE_DRAFT',
              '应用发布门禁'):
    assert token in rel_text, f"补发 workflow 缺 {token}"
# 监视 workflow：独立仓库只检测上游最新 Release 并派发，不做 git merge
sync_text = (wf/'sunuefi-sync-upstream.yml').read_text()
assert 'release_tag_after_sync' in sync_text
for token in ('releases/latest', 'upstream-release', 'client_payload', 'HAS_PAT',
              'force_rebuild', '独立 port 仓库'):
    assert token in sync_text, f"同步 workflow 缺要素: {token}"
# 检查「实际的 merge/fetch 命令」，而不是注释里的说明性文字
assert 'git merge --no-edit' not in sync_text and 'git fetch --no-tags upstream' not in sync_text, \
    '独立仓库的监视 workflow 不应再合并上游源码'
assert 'git remote add upstream' not in sync_text and 'git push origin HEAD:main' not in sync_text, \
    '独立仓库的监视 workflow 不应再配置 upstream remote / 推送合并结果'
for wf_text in (build_text, rel_text):
    assert 'git merge --no-edit' not in wf_text, 'workflow 仍残留 fork merge 语义'
print(f'  {len(files)} 个 workflow：三产物 + 上游 Release 接线 + 发布门禁校验通过')
PY
  pass "Actions YAML（三产物命名 + 需求 7 发布链路）"
else
  echo "  (KIT 模式未落地 workflow？跳过)"
fi

echo "=== 5. 合成输入跑通全链路自检 ==="
ST=""
if [ -n "$KIT" ]; then ST="$KIT/tests/selftest.py"; ARG="$KIT"; else ST="$PORTROOT/tests/selftest.py"; ARG="$(dirname "$PORTROOT")"; fi
if [ -f "$ST" ]; then
  python3 "$ST" "$ARG" || fail "selftest 未通过"
  pass "selftest 通过（ESP/payload/账号/三产物/Release 资产/隔离/分区）"
else
  echo "  (未找到 selftest.py，跳过)"
fi

echo "=== 6. 隔离校验（需求 3）==="
if [ -n "$TARGET" ] && [ -d "$TARGET/port" ]; then
  python3 "$PORTROOT/tools/port-check-isolation.py" --root "$TARGET" --mode tree --json >/dev/null \
    || fail "目标树隔离校验失败"
  pass "目标树自包含校验通过（无上游源码副本）"
elif [ -z "$KIT" ]; then
  python3 "$PORTROOT/tools/port-check-isolation.py" --root "$(dirname "$PORTROOT")" --mode tree --json >/dev/null \
    || fail "port 层自包含校验失败"
  pass "port 层自包含通过"
else
  echo "  (KIT 自检不包含目标树，apply 后传入路径复检)"
fi

echo ""
echo "ALL CHECKS PASSED —— 结构/逻辑校验，不代表真机可启动；真机见 PORTING-MAP.md 第 6 节。"
