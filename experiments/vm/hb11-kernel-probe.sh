#!/bin/sh
set -eu

fail() {
    printf 'HB11_KERNEL_PROBE_FAIL %s\n' "$1"
    exit 1
}

mkdir -p /sys/fs/cgroup
mount -t cgroup2 -o nosuid,nodev,noexec cgroup /sys/fs/cgroup || fail cgroup2-mount

controllers=$(cat /sys/fs/cgroup/cgroup.controllers)
case " $controllers " in *' cpu '*) ;; *) fail missing-cpu-controller ;; esac
case " $controllers " in *' memory '*) ;; *) fail missing-memory-controller ;; esac
case " $controllers " in *' pids '*) ;; *) fail missing-pids-controller ;; esac
printf '+cpu +memory +pids' >/sys/fs/cgroup/cgroup.subtree_control || fail enable-controllers

probe=/sys/fs/cgroup/hb11-kernel-probe
mkdir "$probe" || fail create-probe-cgroup
printf '32M' >"$probe/memory.max" || fail memory-limit
printf '4' >"$probe/pids.max" || fail pids-limit
printf '10000 100000' >"$probe/cpu.max" || fail cpu-limit

sleep 30 &
child=$!
printf '%s' "$child" >"$probe/cgroup.procs" || fail cgroup-admission
grep -q '^populated 1$' "$probe/cgroup.events" || fail cgroup-not-populated
printf '1' >"$probe/cgroup.kill" || fail cgroup-kill
wait "$child" 2>/dev/null || true
grep -q '^populated 0$' "$probe/cgroup.events" || fail cgroup-not-empty

outer_mnt=$(readlink /proc/self/ns/mnt)
outer_net=$(readlink /proc/self/ns/net)
outer_ipc=$(readlink /proc/self/ns/ipc)
unshare -m -n -i sh -c 'printf "%s %s %s\n" "$(readlink /proc/self/ns/mnt)" "$(readlink /proc/self/ns/net)" "$(readlink /proc/self/ns/ipc)" >/run/hb11-probe-namespaces' || fail unshare
read -r inner_mnt inner_net inner_ipc </run/hb11-probe-namespaces
[ "$outer_mnt" != "$inner_mnt" ] || fail mount-namespace-unchanged
[ "$outer_net" != "$inner_net" ] || fail network-namespace-unchanged
[ "$outer_ipc" != "$inner_ipc" ] || fail ipc-namespace-unchanged

mkdir -p /run/hb11-probe-etc
printf 'hbprobe:x:10001:10001:HB probe:/nonexistent:/bin/sh\n' >/run/hb11-probe-etc/passwd
printf 'hbprobe:x:10001:\n' >/run/hb11-probe-etc/group
mount --bind /run/hb11-probe-etc /etc || fail identity-db-bind
mount -o remount,bind,ro /etc || fail identity-db-readonly
su hbprobe -s /bin/sh -c '[ "$(id -u)" = 10001 ] && [ "$(id -g)" = 10001 ]' || fail privilege-drop

printf 'HB11_KERNEL_PROBE cgroup2=cpu,memory,pids cgroup_admit=true cgroup_kill_empty=true namespaces=mount,network,ipc privilege_drop=10001:10001\n'
