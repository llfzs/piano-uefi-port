#!/bin/sh
# SunUEFI 固件的 cmdline 是编译进固件的，固定为：
#     rdinit=/pianoinit piano.root=LABEL=PIANOROOT ... ro
# 上游 Ubuntu 的 initramfs 由 systemd 驱动，只认标准 root=。
# 这里只做两件事：把 piano.root= 翻译成 root=，把固件带的 ro 改成 rw，
# 然后把控制权交回上游 /init（PID 1 不变，systemd 照常接管）。
#
# 上游 initramfs 里没有 mountpoint（只有 util-linux 的 mount），也不保证有 sed，
# 所以下面只用 shell 内建语法，不依赖任何外部命令。
set -eu

# 内核还没挂 /proc 时，/proc/cmdline 读不到。
[ -r /proc/cmdline ] || { mkdir -p /proc; mount -t proc proc /proc; }

spec=""
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        piano.root=*) spec=${arg#piano.root=} ;;
    esac
done
[ -n "$spec" ] || spec=LABEL=PIANOROOT

new=""
have_root=0
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        ro|ro=*) arg=rw ;;   # 固件写死 ro；首次启动要能改 fstab、建 /home
    esac
    case "$arg" in
        root=*) have_root=1 ;;
    esac
    new="$new $arg"
done
[ "$have_root" = 1 ] || new="$new root=$spec"

# 上游自己的 boot.img cmdline 里有这两项，固件的不带，补齐以贴近他们已验证的组合：
#   systemd.gpt_auto=0     —— 别让 systemd 按分区类型自动生成挂载单元，fstab 由我们自己管；
#   msm.separate_gpu_kms=0 —— 上游内核显示路径依赖这个默认值，缺了可能只有黑屏。
for extra in systemd.gpt_auto=0 msm.separate_gpu_kms=0; do
    key=${extra%%=*}
    case " $new " in
        *" $key="*) ;;   # 按前缀判重，避免同名不同值时出现两份
        *) new="$new $extra" ;;
    esac
done
cmdline=${new# }

# systemd-fstab-generator 只读 /proc/cmdline，用 bind mount 提供改写后的副本。
printf '%s\n' "$cmdline" > /.piano-cmdline
mount --bind /.piano-cmdline /proc/cmdline

printf 'piano: effective cmdline: %s\n' "$cmdline" > /dev/kmsg 2>/dev/null || true
exec /init
