# havenbox
安全、易用的桌面 Agent 沙箱，支持主流 Agent 与 MCP，敏感凭证无需进入沙箱。

## VM 模块

HB-02 smoke guest 的 initramfs 加载六个模块：`virtio_blk`、`qemu_fw_cfg`、`crc16`、`mbcache`、`jbd2` 和 `ext4`。
