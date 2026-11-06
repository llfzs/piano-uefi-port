#!/bin/sh
# 上游 rootfs 的 fstab 指向它自己的分区方案（PARTLABEL=linux_rootfs / linux_data）。
# 本机沿用既有 sunuefi_esp / sunuefi_root，这两块分区不存在，systemd 会因挂载失败
# 拖住 local-fs.target。首次启动时把它们注释掉并补出 /home，之后不再改动用户的 fstab。
set -eu

root=/sysroot
marker="$root/.piano-firstboot-done"

[ -d "$root/etc" ] || exit 0
[ -e "$marker" ] && exit 0

if [ -f "$root/etc/fstab" ]; then
    if grep -qE 'linux_rootfs|linux_data' "$root/etc/fstab"; then
        [ -e "$root/etc/fstab.piano-orig" ] || cp "$root/etc/fstab" "$root/etc/fstab.piano-orig"
        sed -i -E 's|^([^#].*(linux_rootfs|linux_data).*)$|#\1|' "$root/etc/fstab"
        echo 'piano: commented linux_rootfs/linux_data in fstab' > /dev/kmsg 2>/dev/null || true
    fi
fi

mkdir -p "$root/home"
: > "$marker"
echo 'piano: rootfs first-boot fixup done' > /dev/kmsg 2>/dev/null || true
