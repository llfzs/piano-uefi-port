#!/bin/sh
# 从备份目录恢复 ESP 四件套。
# 用法（在平板上，root）：sh rollback-esp.sh /home/piano/esp-backup-<时间戳>
set -eu

SRC=${1:?用法: sh rollback-esp.sh <备份目录>}
DST=/boot/efi/EFI/Piano/stable

[ -d "$SRC" ] || { echo "备份目录不存在: $SRC" >&2; exit 1; }

for f in Image board.dtb initramfs boot.img; do
    [ -f "$SRC/$f" ] || { echo "备份里缺少 $f" >&2; exit 1; }
done

# 有清单就先校验，避免拿半截/损坏的备份把 ESP 写坏。
if [ -f "$SRC/SHA256SUMS" ]; then
    ( cd "$SRC" && sha256sum -c SHA256SUMS ) || { echo "备份校验失败，未写入" >&2; exit 1; }
fi

mkdir -p "$DST"
for f in Image board.dtb initramfs boot.img; do
    cp "$SRC/$f" "$DST/$f"
done
sync

echo "已恢复："
for f in Image board.dtb initramfs boot.img; do
    sha256sum "$DST/$f"
done
