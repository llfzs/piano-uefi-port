#!/bin/sh
# 上游 rootfs 的 fstab 指向它自己的分区方案（PARTLABEL=linux_rootfs / linux_data）。
# 本机沿用既有 sunuefi_esp / sunuefi_root，这两块分区不存在，systemd 会因挂载失败
# 拖住 local-fs.target。首次启动时把它们注释掉并补出 /home，之后不再改动用户的 fstab。
#
# 这个脚本跑在上游 initramfs 里，只用 shell 内建 + cat/rm/mkdir（镜像里确认存在），
# 不用 grep/sed/cp：这类工具在裁剪过的 initrd 里经常缺席，缺了会静默失败，
# 而 fstab 没改会直接把启动拖进 emergency。
set -eu

root=/sysroot
marker="$root/.piano-firstboot-done"

[ -d "$root/etc" ] || exit 0
[ -e "$marker" ] && exit 0

if [ -f "$root/etc/fstab" ]; then
    [ -e "$root/etc/fstab.piano-orig" ] || cat "$root/etc/fstab" > "$root/etc/fstab.piano-orig"

    changed=0
    : > "$root/etc/fstab.piano-new"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            \#*|"") printf '%s\n' "$line" >> "$root/etc/fstab.piano-new" ;;
            *linux_rootfs*|*linux_data*)
                printf '#%s\n' "$line" >> "$root/etc/fstab.piano-new"
                changed=1
                ;;
            *) printf '%s\n' "$line" >> "$root/etc/fstab.piano-new" ;;
        esac
    done < "$root/etc/fstab"

    if [ "$changed" = 1 ]; then
        cat "$root/etc/fstab.piano-new" > "$root/etc/fstab"
        echo 'piano: commented linux_rootfs/linux_data in fstab' > /dev/kmsg 2>/dev/null || true
    fi
    rm -f "$root/etc/fstab.piano-new"
fi

mkdir -p "$root/home"
: > "$marker"
echo 'piano: rootfs first-boot fixup done' > /dev/kmsg 2>/dev/null || true
