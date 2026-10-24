import CryptoKit
import Foundation

/// Preparation is deliberately separate from execution eligibility. A prepared VM is not a grant.
public struct RuntimeManager {
    public struct Asset {
        public let url: URL
        public let sha256: String
        public init(url: URL, sha256: String) { self.url = url; self.sha256 = sha256 }
    }
    public struct Prepared {
        public let executable: URL
        public let arguments: [String]
    }
    public init() {}

    private func verified(_ asset: Asset) throws -> String {
        let url = asset.url.standardizedFileURL
        guard url.path == asset.url.path, url.path.hasPrefix("/"),
              !url.path.contains(","), !url.path.contains("\n"),
              url.resolvingSymlinksInPath().path == url.path,
              asset.sha256.count == 64,
              asset.sha256.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
            throw RuntimeError.assetRejected("noncanonical asset path or digest: \(asset.url.path) -> \(url.path) -> \(url.resolvingSymlinksInPath().path)")
        }
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true else {
            throw RuntimeError.assetRejected("asset is not a regular file")
        }
        let digest = SHA256.hash(data: try Data(contentsOf: url)).map { String(format: "%02x", $0) }.joined()
        guard digest == asset.sha256 else { throw RuntimeError.assetRejected("asset digest mismatch") }
        return url.path
    }

    private struct FileIdentity: Equatable {
        let device: UInt64
        let inode: UInt64
    }
    private func identity(_ path: String) throws -> FileIdentity {
        let attributes = try FileManager.default.attributesOfItem(atPath: path)
        guard attributes[.type] as? FileAttributeType == .typeRegular,
              let device = attributes[.systemNumber] as? NSNumber,
              let inode = attributes[.systemFileNumber] as? NSNumber else {
            throw RuntimeError.invalidConfiguration
        }
        return FileIdentity(device: device.uint64Value, inode: inode.uint64Value)
    }

    /// HB-02 machine/accelerator/disk topology, with three dedicated virtio serial ports.
    /// Caller supplies trusted installed asset digests, never guest-provided manifests.
    /// No extra arguments, network devices, host sharing, QMP or fw_cfg executable payload.
    public func prepare(qemu: Asset, kernel: Asset, initrd: Asset, root: Asset,
                        data: URL, privateDirectory: URL) throws -> Prepared {
        let executable = try verified(qemu)
        let kernelPath = try verified(kernel), initrdPath = try verified(initrd), rootPath = try verified(root)
        let directory = privateDirectory.standardizedFileURL
        let attributes = try FileManager.default.attributesOfItem(atPath: directory.path)
        guard directory.resolvingSymlinksInPath() == directory,
              attributes[.type] as? FileAttributeType == .typeDirectory,
              (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              data.path == data.standardizedFileURL.path,
              data.resolvingSymlinksInPath() == data,
              !data.path.contains(","), !data.path.contains("\n"),
              ![executable, kernelPath, initrdPath, rootPath].contains(data.path),
              try data.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else {
            throw RuntimeError.invalidConfiguration
        }
        // Distinct paths can name the same inode through hard links. This is a
        // preparation-time check, not a TOCTOU-safe installed-asset launch path.
        let dataIdentity = try identity(data.path)
        for path in [executable, kernelPath, initrdPath, rootPath] {
            guard try identity(path) != dataIdentity else { throw RuntimeError.invalidConfiguration }
        }
        var args = ["-M", "virt", "-accel", "hvf", "-cpu", "host", "-smp", "2", "-m", "2048M",
                    "-nodefaults", "-nographic", "-nic", "none", "-serial", "null", "-monitor", "none",
                    "-kernel", kernelPath, "-initrd", initrdPath,
                    "-append", "console=ttyAMA0 root=/dev/vda ro init=/sbin/hb-init",
                    "-drive", "if=virtio,format=raw,file=\(rootPath),readonly=on",
                    "-drive", "if=virtio,format=raw,file=\(data.path),snapshot=off",
                    "-device", "virtio-serial-device,id=hbserial"]
        for channel in ["control", "file", "inference"] {
            let socket = directory.appendingPathComponent(channel + ".sock").path
            guard !socket.contains(","), socket.utf8.count < 100,
                  !FileManager.default.fileExists(atPath: socket) else { throw RuntimeError.invalidConfiguration }
            args += ["-chardev", "socket,id=\(channel),path=\(socket),server=on,wait=off",
                     "-device", "virtserialport,bus=hbserial.0,chardev=\(channel),name=hb.\(channel)"]
        }
        return Prepared(executable: URL(fileURLWithPath: executable), arguments: args)
    }

    /// Closed until launchd ownership, installed immutable image, and live transport are validated.
    public func start(_ prepared: Prepared) throws -> Never {
        throw RuntimeError.unavailable("HB-11 launchd/image/transport integration not validated; no process started")
    }
}
