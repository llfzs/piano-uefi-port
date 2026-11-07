#!/bin/sh
# 把构建好的四件套写入现有 ESP。先整目录备份到 /home，再覆盖。
# 用法（在平板上，root）：sh deploy-esp.sh <源目录>
set -eu

SRC=${1:-/tmp/esp-tree/EFI/Piano/stable}
DST=/boot/efi/EFI/Piano/stable
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/home/piano/esp-backup-$STAMP

[ -d "$SRC" ] || { echo "源目录不存在: $SRC" >&2; exit 1; }
for f in Image board.dtb initramfs boot.img; do
    [ -f "$SRC/$f" ] || { echo "缺少 $SRC/$f" >&2; exit 1; }
done

# ESP 是 FAT32：cp -a 会带一堆 FAT 表达不了的时间戳/属性，用 -r 就够，
# 而且首次部署时 DST 可能还不存在（cp 加 set -e 会直接退出）。
mkdir -p "$BACKUP"
if [ -d "$DST" ] && [ -n "$(ls -A "$DST" 2>/dev/null)" ]; then
    cp -r "$DST"/. "$BACKUP"/
    ( cd "$BACKUP" && sha256sum ./* > SHA256SUMS )
    echo "已备份到 $BACKUP"
else
    echo "警告：$DST 为空或不存在，没有旧文件可备份（首次部署）" >&2
fi

mkdir -p "$DST"
for f in Image board.dtb initramfs boot.img; do
    cp "$SRC/$f" "$DST/$f"
done
sync

echo "写入完成，回读校验："
for f in Image board.dtb initramfs boot.img; do
    a=$(sha256sum "$SRC/$f" | cut -d' ' -f1)
    b=$(sha256sum "$DST/$f" | cut -d' ' -f1)
    [ "$a" = "$b" ] || { echo "校验失败: $f" >&2; exit 1; }
    echo "  OK $f $a"
done
echo "回退：sh rollback-esp.sh $BACKUP"
