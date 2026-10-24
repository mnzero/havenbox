#!/bin/sh
set -eu
# Guest-side payload contract: fw_cfg key opt/havenbox.payload.
# The final Linux initramfs must invoke this script from the guest.
printf '%s\n' 'HB02 guest payload: control=file-transfer-probe; persistent-data=/dev/vdb; network=disabled'
