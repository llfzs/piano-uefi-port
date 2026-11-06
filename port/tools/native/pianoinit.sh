#!/bin/sh
# SunUEFI 固件的 cmdline 是编译进固件的，固定为：
#     rdinit=/pianoinit piano.root=LABEL=PIANOROOT ... ro
# 上游 Ubuntu 的 initramfs 由 systemd 驱动，只认标准 root=。
# 这里只做两件事：把 piano.root= 翻译成 root=，把固件带的 ro 改成 rw，
# 然后把控制权交回上游 /init（PID 1 不变，systemd 照常接管）。
set -eu

mkdir -p /proc
mountpoint -q /proc || mount -t proc proc /proc

spec=""
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        piano.root=*) spec=${arg#piano.root=} ;;
    esac
done
[ -n "$spec" ] || spec=LABEL=PIANOROOT

cmdline=$(cat /proc/cmdline)
case " $cmdline " in
    *" root="*) ;;
    *) cmdline="$cmdline root=$spec" ;;
esac
cmdline=$(printf '%s' "$cmdline" | sed -e 's/ ro / rw /g' -e 's/^ro /rw /')

# systemd-fstab-generator 只读 /proc/cmdline，用 bind mount 提供改写后的副本。
printf '%s\n' "$cmdline" > /.piano-cmdline
mount --bind /.piano-cmdline /proc/cmdline

printf 'piano: effective cmdline: %s\n' "$cmdline" > /dev/kmsg 2>/dev/null || true
exec /init
