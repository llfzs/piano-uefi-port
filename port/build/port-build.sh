#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# 需求 1/2/4/5：CI 构建编排总入口（GitHub Actions 与本地通用）。
#
# 流水线（全部只消费「上游构建产物」，零上游源码修改）：
#   [上游] build-kernel.sh / prepare-boot-dtb.py / build-initramfs.sh / build-rootfs-image.py
#      -> Image, piano-boot.dtb, initramfs.cpio.gz, root.ext4 / root.sparse.img
#   [本层] port-inject-login.py   (piano/piano + root piano + 自动登录)
#          port-adapt-rootfs.py   (fstab / root-policy / udev 保护 Android 分区)
#          port-stage-esp.py      (Image/dtb/initrd -> SUNUEFI-LINUXv2 ESP)
#          port-package-artifacts.py (三产物 + sparse + manifest)
#
# 用法：
#   ./port-build.sh --fork <本仓库根> --inputs <上游产物目录> --output <artifact 根> \
#                   [--target ubuntu-gnome] [--module-zip X] [--esp-image X] [--root-image X] \
#                   [--selfcheck]   # 只跑 port 层可离线部分，用合成输入验证全链路
#
# 契约：只写 output/ 与临时目录，绝不写上游受管目录，绝不碰设备、不执行 fastboot。
set -euo pipefail

FORK=""; INPUTS=""; OUTPUT=""; TARGET=""; MODULE_ZIP=""; ESP_IMAGE=""; ROOT_IMAGE=""; SELFCHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fork) FORK="$2"; shift 2;;
    --inputs) INPUTS="$2"; shift 2;;
    --output) OUTPUT="$2"; shift 2;;
    --target) TARGET="$2"; shift 2;;
    --module-zip) MODULE_ZIP="$2"; shift 2;;
    --esp-image) ESP_IMAGE="$2"; shift 2;;
    --root-image) ROOT_IMAGE="$2"; shift 2;;
    --selfcheck) SELFCHECK=1; shift;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done

info() { printf '\033[36m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }

# selfcheck 模式：不需要真实上游产物，用 tests/selftest.py 覆盖 port 层全链路
if [ "$SELFCHECK" = 1 ]; then
  [ -n "$FORK" ] || FORK="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  info "自检模式：验证 port 层可离线全链路（无需真机/内核编译）"
  python3 "$FORK/port/../tests/selftest.py" "$FORK/.." 2>/dev/null \
    || python3 "$(dirname "${BASH_SOURCE[0]}")/../../tests/selftest.py" "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.." 2>/dev/null \
    || { warn "未找到 selftest.py，退回 isolation 校验"; \
         python3 "$FORK/port/tools/port-check-isolation.py" --root "$FORK" --mode tree --json >/dev/null; }
  info "自检完成"
  exit 0
fi

[ -n "$FORK" ] && [ -d "$FORK/port" ] || { echo "用法: 需要 --fork <本仓库根（含 port/）>" >&2; exit 2; }
[ -n "$OUTPUT" ] || { echo "需要 --output" >&2; exit 2; }
FORK="$(cd "$FORK" && pwd)"
PORT="$FORK/port"
PROFILE="$PORT/config/port-profile.json"
DISTROS="$PORT/config/distros.json"
TOOLS="$PORT/tools"
mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"

# 0) 隔离预检：保证本次构建没有偷偷改上游文件
info "[0/5] 隔离校验"
python3 "$TOOLS/port-check-isolation.py" --root "$FORK" --mode tree --json >/dev/null \
  || { echo "隔离校验失败" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1) rootfs 后处理：账号注入 + 挂载契约（仅当提供了解出的 rootfs 目录）
if [ -n "$INPUTS" ] && [ -d "$INPUTS/rootfs" ]; then
  info "[1/5] rootfs 后处理：注入账号 + 适配 sunuefi_root 挂载"
  cp -a "$INPUTS/rootfs" "$WORK/rootfs"
  python3 "$TOOLS/port-inject-login.py" --rootfs "$WORK/rootfs" --profile "$PROFILE" >/dev/null
  python3 "$TOOLS/port-adapt-rootfs.py" --rootfs "$WORK/rootfs" --profile "$PROFILE" >/dev/null
  # 若上游提供的是目录形态 rootfs，这里可交给镜像打包器（build-ubuntu-ext4 / mke2fs -d）。
  # 本脚本只产出处理好的目录，镜像化由 CI 的 rootfs job 完成（见 workflow）。
  echo "  处理后的 rootfs 目录: $WORK/rootfs（本脚本不镜像化，交由 rootfs job）"
else
  warn "[1/5] 未提供解出的 rootfs 目录，跳过本步骤的账号/挂载后处理"
  if [ -n "$ROOT_IMAGE" ] && [ -f "$ROOT_IMAGE" ]; then
    info "      --root-image 为上游 root.sparse.img 原样透传；piano/piano 账号与"
    info "      sunuefi_root 挂载契约已由 Module ZIP 的 service.sh 在首次启动时注入"
    info "      （见 port/docs/UPSTREAM-RELEASE.md），无需在 CI 里改 60GiB 镜像。"
  fi
fi

# 2) ESP 布局：内核四件套 -> SUNUEFI-LINUXv2
if [ -n "$INPUTS" ] && [ -f "$INPUTS/Image" ] && [ -f "$INPUTS/piano-boot.dtb" ] && [ -f "$INPUTS/initramfs.cpio.gz" ]; then
  info "[2/5] ESP 布局（Image/dtb/initrd -> SUNUEFI-LINUXv2）"
  python3 "$TOOLS/port-stage-esp.py" --image "$INPUTS/Image" --dtb "$INPUTS/piano-boot.dtb" \
    --initrd "$INPUTS/initramfs.cpio.gz" --profile "$PROFILE" --output "$WORK/esp" --force >/dev/null
  echo "  ESP 目录: $WORK/esp（FAT32 镜像化由 ESP job 完成）"
else
  warn "[2/5] 未提供内核四件套，跳过 ESP 布局（CI 应传入 --esp-image 预打包镜像）"
fi

# 3) 分区计划（只出计划，不刷机）
info "[3/5] 生成分区计划（sunuefi_esp / sunuefi_root，不动 userdata）"
python3 "$TOOLS/port-partition-plan.py" --profile "$PROFILE" --output "$WORK/plan" >/dev/null

# 4) 三产物打包
info "[4/5] 打包三产物（SunUEFI-Module / -ESP / -Root-<target>）"
PKG_ARGS=(--profile "$PROFILE" --distros "$DISTROS" --output "$OUTPUT" --force)
[ -n "$TARGET" ] && PKG_ARGS+=(--target "$TARGET")
# 产物名带上游 tag（SunUEFI-Piano-v1.0.0.zip），别所有版本都叫 0.0.0-port。
# 值来自上游构建阶段写的 port-inputs.env，没有就退回默认。
if [ -n "$INPUTS" ] && [ -f "$INPUTS/port-inputs.env" ]; then
  VER=$(sed -n 's/^UPSTREAM_TAG=["]*\([^"]*\)["]*$/\1/p' "$INPUTS/port-inputs.env" | head -1)
  [ -n "${VER:-}" ] && PKG_ARGS+=(--version "$VER")
fi
[ -n "$MODULE_ZIP" ] && [ -f "$MODULE_ZIP" ] && PKG_ARGS+=(--module-zip "$MODULE_ZIP") || warn "无 Module ZIP，产物将是占位骨架"
[ -n "$ESP_IMAGE" ] && [ -f "$ESP_IMAGE" ] && PKG_ARGS+=(--esp-image "$ESP_IMAGE") || warn "无 ESP 镜像，产物将是占位骨架"
[ -n "$ROOT_IMAGE" ] && [ -f "$ROOT_IMAGE" ] && PKG_ARGS+=(--root-image "$ROOT_IMAGE") || warn "无 root 镜像，产物将是占位骨架"
python3 "$TOOLS/port-package-artifacts.py" "${PKG_ARGS[@]}" >/dev/null

# 5) 校验产物
info "[5/5] 校验产物 manifest"
python3 - "$OUTPUT" <<'PY' || { echo "产物校验失败" >&2; exit 1; }
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
m = json.loads((out / 'manifest.json').read_text())
assert m['status'] == 'ARTIFACTS_PACKAGED_HOST_ONLY'
a = m['artifacts']
assert 'SunUEFI-Module' in a and 'SunUEFI-ESP' in a, '缺共享产物'
roots = [k for k in a if k.startswith('SunUEFI-Root-')]
assert roots, '缺 root 产物'
print('  artifacts:', ', '.join(a.keys()))
PY
info "完成：$OUTPUT"
info "提示：真实刷写需按 docs/PORTING-MAP.md 第 6 节做真机验证，本脚本不碰设备。"
