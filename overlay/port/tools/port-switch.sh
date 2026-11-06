#!/system/bin/sh
# SPDX-License-Identifier: MIT
# 桥接层：系统切换。对齐 SunUEFI uefi/handoff/bootselect/BootRequest.c 的语义。
#
# 源仓库旧方式：piano-boot-switch 把整组 boot 三元组写回 A 槽 -> 需要重刷分区、锁 A 槽。
# SunUEFI 新方式：只在 BOOT 分区记录区写一个 64B BootRequest 记录（CRC32 + 双页序号），
#   目标 android(0)/uefi(1)/linux(2)/setup(3)，持久生效直到再次选择，不写 A 槽三元组、不碰 vbmeta。
#
# 本脚本是"接线模板"：真正的写入由 SunUEFI 的 piano-boot-repack / piano-boot-request
# 工具完成（它们在 Android 模块里以 native ELF 提供）。这里给出正确的调用序列与目标枚举，
# 避免把源仓库的 piano-boot-switch 语义误接进来。
set -eu

TOOL="${PIANO_BOOT_TOOL:-piano-boot-repack}"
POLICY="${PIANO_BOOT_POLICY:-/data/adb/modules/piano_sunuefi/policy.json}"
STATE="${PIANO_BOOT_STATE:-/data/adb/piano-sunuefi/install-state.json}"

usage() {
  echo "用法: $0 <status|to-android|to-linux|menu|setup> [--reboot]" >&2
  echo "  切换目标在 BOOT 记录区持久保存，除非再次选择；不改动 A 槽 boot 三元组。" >&2
}

[ $# -ge 1 ] || { usage; exit 2; }
op="$1"; shift
reboot_flag=""
[ "${1:-}" = "--reboot" ] && reboot_flag=1

bootdev() {
  slot="$(getprop ro.boot.slot_suffix)"
  echo "/dev/block/by-name/boot${slot}"
}

case "$op" in
  status)
    "$TOOL" status --policy "$POLICY" --boot-device "$(bootdev)" \
      --active-slot "$(getprop ro.boot.slot_suffix)" \
      --installed-state "$STATE" --json --read-only
    ;;
  to-android|to-linux|menu|setup)
    case "$op" in
      to-android) target=android ;;
      to-linux)   target=linux ;;
      menu)       target=uefi ;;
      setup)      target=setup ;;
    esac
    # 先只读 preview，再执行；--execute 前必须已通过 SunUEFI 的 policy/设备证明门禁。
    "$TOOL" request --target "$target" --boot-device "$(bootdev)" \
      --active-slot "$(getprop ro.boot.slot_suffix)" --policy "$POLICY" \
      --installed-state "$STATE" --preview
    "$TOOL" request --target "$target" --boot-device "$(bootdev)" \
      --active-slot "$(getprop ro.boot.slot_suffix)" --policy "$POLICY" \
      --installed-state "$STATE" --execute
    [ -z "$reboot_flag" ] || reboot
    ;;
  *) usage; exit 2 ;;
esac
