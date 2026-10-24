#!/bin/sh
set -eu

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

fail() {
    printf 'HB02_GUEST_FAIL %s\n' "$1"
    while :; do
        sleep 3600
    done
}

root_source=$(awk '$2 == "/" { print $1; exit }' /proc/mounts 2>/dev/null || true)
root_options=$(awk '$2 == "/" { print $4; exit }' /proc/mounts 2>/dev/null || true)
mount_has_option() {
    case ",$1," in
        *",$2,"*) return 0 ;;
        *) return 1 ;;
    esac
}
if [ "$root_source" != /dev/vda ]; then
    mkdir -p /dev /proc /sys /run /tmp /newroot
    mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t tmpfs tmpfs /run
    insmod /lib/modules/virtio_blk.ko.gz || fail 'virtio-blk-module-load-failed'
    insmod /lib/modules/qemu_fw_cfg.ko.gz || fail 'qemu-fw-cfg-module-load-failed'
    insmod /lib/modules/crc16.ko.gz || fail 'crc16-module-load-failed'
    insmod /lib/modules/mbcache.ko.gz || fail 'mbcache-module-load-failed'
    insmod /lib/modules/jbd2.ko.gz || fail 'jbd2-module-load-failed'
    insmod /lib/modules/ext4.ko.gz || fail 'ext4-module-load-failed'
    for unused in 1 2 3 4 5; do
        [ -e /dev/vda ] && [ -e /dev/vdb ] && break
        sleep 1
    done
    [ -e /dev/vda ] || fail 'missing-readonly-root-disk'
    [ -e /dev/vdb ] || fail 'missing-persistent-data-disk'
    mount -o ro /dev/vda /newroot || fail 'root-mount-failed'
    newroot_source=$(awk '$2 == "/newroot" { print $1; exit }' /proc/mounts)
    newroot_options=$(awk '$2 == "/newroot" { print $4; exit }' /proc/mounts)
    [ "$newroot_source" = /dev/vda ] || fail 'root-device-is-not-vda'
    mount_has_option "$newroot_options" ro || fail 'root-mount-not-readonly'
    mount -o move /dev /newroot/dev || fail 'dev-move-failed'
    mount -o move /proc /newroot/proc || fail 'proc-move-failed'
    mount -o move /sys /newroot/sys || fail 'sys-move-failed'
    mount -o move /run /newroot/run || fail 'run-move-failed'
    exec switch_root /newroot /sbin/hb02-guest-init
fi

[ "$root_source" = /dev/vda ] || fail 'root-device-is-not-vda'
[ "$(awk '$2 == "/" { print $2; exit }' /proc/mounts)" = / ] || fail 'root-mountpoint-is-not-root'
mount_has_option "$root_options" ro || fail 'root-mount-not-readonly'
[ -e /dev/vdb ] || fail 'missing-persistent-data-disk'
[ "$(cat /sys/block/vdb/ro 2>/dev/null || true)" = 0 ] || fail 'data-disk-is-readonly'

nonce_path=/sys/firmware/qemu_fw_cfg/by_name/opt/havenbox.nonce/raw
payload_path=/sys/firmware/qemu_fw_cfg/by_name/opt/havenbox.payload/raw
[ -r "$nonce_path" ] || fail 'missing-fw-cfg-nonce'
[ -r "$payload_path" ] || fail 'missing-fw-cfg-payload'
nonce=$(tr -d '\000\r\n' < "$nonce_path")
[ "${#nonce}" -eq 32 ] || fail 'invalid-fw-cfg-nonce'
case "$nonce" in *[!0-9a-f]*) fail 'invalid-fw-cfg-nonce' ;; esac
cp "$payload_path" /run/havenbox-payload.sh
chmod 500 /run/havenbox-payload.sh
/run/havenbox-payload.sh || fail 'payload-execution-failed'
network_devices=
[ -e /sys/class/net/lo ] || fail 'missing-loopback-device'
for device in /sys/class/net/*; do
    [ -e "$device" ] || continue
    name=${device##*/}
    [ "$name" = lo ] || network_devices="$network_devices $name"
done
[ -z "$network_devices" ] || fail "unexpected-network-device:$network_devices"
grep -Eq ' (9p|virtiofs) ' /proc/mounts && fail 'unexpected-shared-filesystem'
marker=HB02_PERSISTENCE_V1
persisted=$(dd if=/dev/vdb bs=1 count=19 2>/dev/null || true)
if [ "$persisted" = "$marker" ]; then persistence=present; else
    printf %s "$marker" | dd of=/dev/vdb bs=1 conv=fsync 2>/dev/null || fail 'data-disk-write-failed'
    persistence=created
fi
printf 'HB02_GUEST_PROBE network=loopback-only root_readonly=true root_device=/dev/vda data_device=/dev/vdb persistence=%s shared_mounts=none\n' "$persistence"
printf 'HB02_GUEST_READY %s\n' "$nonce"
while :; do sleep 3600; done
