#!/bin/sh
# SunUEFI 固件的 cmdline 是编译进固件的，固定为 rdinit=/pianoinit piano.root=LABEL=PIANOROOT。
# 上游 Ubuntu 的 initramfs 不认识 piano.root=，所以这里提供一个最小 init：
# 拉起存储链路 → 按 GPT 分区名 + LABEL 找到 sunuefi_root → switch_root 到真实系统。
export PATH=/usr/sbin:/usr/bin:/bin:/sbin

log() {
    printf 'piano: %s\n' "$*"
    [ -w /dev/kmsg ] && printf '<5>piano: %s\n' "$*" >/dev/kmsg 2>/dev/null
    return 0
}
die() {
    log "PIANOINIT_FAILED: $*"
    # 留在 initramfs 里给一个 shell，便于串口/USB 排查。
    exec /bin/sh
}

mkdir -p /dev /proc /sys /run /sysroot /tmp
mountpoint -q /dev || mount -t devtmpfs devtmpfs /dev || die devtmpfs
mountpoint -q /proc || mount -t proc proc /proc || die proc
mountpoint -q /sys || mount -t sysfs sysfs /sys || die sysfs
mountpoint -q /run || mount -t tmpfs -o mode=0755 tmpfs /run || die run

spec=LABEL=PIANOROOT
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        piano.root=*) spec=${arg#piano.root=} ;;
    esac
done
log "root spec: $spec"

# 上游 ramdisk 自带 modules.dep 时按依赖拉起存储链路；缺哪个跳哪个，不阻断启动。
release=$(uname -r)
if [ -d "/lib/modules/$release" ]; then
    for module in arm_smmu phy_qcom_qmp_ufs pinctrl_sm8750 ufshcd_pltfrm ufs_qcom; do
        modprobe "$module" 2>/dev/null && log "module $module loaded"
    done
fi

find_root() {
    for part in /sys/class/block/*; do
        [ -f "$part/partition" ] || continue
        name=$(sed -n 's/^PARTNAME=//p' "$part/uevent" 2>/dev/null)
        [ "$name" = sunuefi_root ] || continue
        candidate=/dev/${part##*/}
        [ -b "$candidate" ] || continue
        printf '%s\n' "$candidate"
        return 0
    done
    return 1
}

dev=""
tries=0
while [ "$tries" -lt 30 ]; do
    dev=$(find_root) && break
    sleep 1
    tries=$((tries + 1))
done
[ -n "$dev" ] || die "sunuefi_root did not appear"

log "root device: $dev"
mount -t ext4 -o rw "$dev" /sysroot || die "ext4 mount failed"
if [ ! -x /sysroot/sbin/init ] && [ ! -x /sysroot/usr/lib/systemd/systemd ]; then
    die "root has no init"
fi

# 上游 rootfs 的 fstab 指向 linux_rootfs / linux_data（它自己的分区方案）。
# 本机这两块分区不存在，systemd 会因为挂载失败拖住 local-fs.target。
# 首次启动时把这两项注释掉，并确保 /home 存在，之后再不动用户的 fstab。
if [ ! -f /sysroot/.piano-firstboot-done ]; then
    if grep -qE 'linux_rootfs|linux_data' /sysroot/etc/fstab 2>/dev/null; then
        sed -i -E 's|^([^#].*(linux_rootfs|linux_data).*)$|#\1|' /sysroot/etc/fstab || die "fstab fix failed"
        log "fstab: commented linux_rootfs/linux_data entries"
    fi
    mkdir -p /sysroot/home || die "cannot create /home"
    : > /sysroot/.piano-firstboot-done || die "cannot write firstboot marker"
fi

# 按 SunUEFI 既有顺序搬移虚拟文件系统，行为与现网 init 保持一致。
for virtual in dev proc sys run; do
    mount --move "/$virtual" "/sysroot/$virtual" || die "cannot move $virtual"
done

log "PIANOINIT_READY: switch_root to $dev"
exec switch_root /sysroot /sbin/init
