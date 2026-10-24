import CryptoKit
import Foundation
import HavenboxCore

@main struct RuntimeTests {
    static func main() throws {
        let instance = UUID()
        var lease = try RuntimeWatchdog(instance: instance, epoch: 7, now: 0, ttl: 100)
        try lease.hello(instance: instance, epoch: 7, now: 1)
        try lease.heartbeat(sequence: 1, now: 2)
        precondition(lease.tick(now: 101))
        precondition(!lease.tick(now: 102))
        do { try lease.heartbeat(sequence: 2, now: 103); fatalError("revived expired lease") } catch {}
        var wrong = try RuntimeWatchdog(instance: instance, epoch: 7, now: 0, ttl: 100)
        do { try wrong.hello(instance: UUID(), epoch: 7, now: 1); fatalError("wrong instance") } catch {}
        precondition(!wrong.tick(now: 2))
        var replay = try RuntimeWatchdog(instance: instance, epoch: 7, now: 0, ttl: 100)
        try replay.hello(instance: instance, epoch: 7, now: 1)
        try replay.heartbeat(sequence: 1, now: 2)
        do { try replay.heartbeat(sequence: 1, now: 3); fatalError("replayed heartbeat") } catch {}
        precondition(!replay.tick(now: 4))

        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".hb11-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let bytes = Data("synthetic asset, never executed".utf8)
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        func asset(_ name: String, executable: Bool = false, writable: Bool = false) throws -> RuntimeManager.Asset {
            let url = directory.appendingPathComponent(name)
            try bytes.write(to: url)
            try FileManager.default.setAttributes(
                [.posixPermissions: executable ? 0o500 : (writable ? 0o600 : 0o400)],
                ofItemAtPath: url.path
            )
            return RuntimeManager.Asset(path: url.path, sha256: digest)
        }
        let qemu = try asset("qemu", executable: true)
        let qemuFramework = try asset("qemu-framework", executable: true)
        let kernel = try asset("Image"), initrd = try asset("initrd"), root = try asset("root.raw")
        let data = URL(fileURLWithPath: try asset("data.raw", writable: true).path)
        let manager = RuntimeManager()
        let plan = try manager.prepare(qemu: qemu, qemuFramework: qemuFramework,
                                       kernel: kernel, initrd: initrd, root: root,
                                       data: data, privateDirectory: directory)
        let directoryHint = URL(fileURLWithPath: directory.path, isDirectory: true)
        _ = try manager.prepare(qemu: qemu, qemuFramework: qemuFramework,
                                kernel: kernel, initrd: initrd, root: root,
                                data: data, privateDirectory: directoryHint)
        precondition(plan.arguments.contains("none") && !plan.arguments.contains("-netdev"))
        precondition(!plan.arguments.contains("-qmp") && !plan.arguments.contains("-fw_cfg"))
        precondition(plan.arguments.filter { $0.contains("name=hb.") }.count == 3)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: kernel.path)
        do {
            _ = try manager.prepare(qemu: qemu, qemuFramework: qemuFramework,
                                    kernel: kernel, initrd: initrd, root: root,
                                    data: data, privateDirectory: directory)
            fatalError("owner-writable immutable asset accepted")
        } catch RuntimeError.assetRejected { }
        try FileManager.default.setAttributes([.posixPermissions: 0o400], ofItemAtPath: kernel.path)
        // A distinct pathname can alias any read-only asset's actual inode.
        for (index, readOnly) in [qemu, qemuFramework, kernel, initrd, root].enumerated() {
            let alias = directory.appendingPathComponent("hardlink-\(index).raw")
            try FileManager.default.linkItem(at: URL(fileURLWithPath: readOnly.path), to: alias)
            do {
                _ = try manager.prepare(qemu: qemu, qemuFramework: qemuFramework,
                                        kernel: kernel, initrd: initrd, root: root,
                                        data: alias, privateDirectory: directory)
                fatalError("hardlinked data accepted for \(URL(fileURLWithPath: readOnly.path).lastPathComponent)")
            } catch RuntimeError.invalidConfiguration { }
              catch RuntimeError.assetRejected { }
            try FileManager.default.removeItem(at: alias)
        }
        precondition(plan.configuration.epoch == 1 && plan.configuration.role == "harness")
        try FileManager.default.setAttributes([.posixPermissions: 0o400], ofItemAtPath: data.path)
        do {
            _ = try manager.prepare(qemu: qemu, qemuFramework: qemuFramework,
                                    kernel: kernel, initrd: initrd, root: root,
                                    data: data, privateDirectory: directory)
            fatalError("read-only data disk accepted")
        } catch RuntimeError.invalidConfiguration { }
          catch RuntimeError.assetRejected { }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: data.path)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: root.path)
        try Data("changed".utf8).write(to: URL(fileURLWithPath: root.path))
        try FileManager.default.setAttributes([.posixPermissions: 0o400], ofItemAtPath: root.path)
        do {
            _ = try manager.prepare(qemu: qemu, qemuFramework: qemuFramework,
                                    kernel: kernel, initrd: initrd, root: root,
                                    data: data, privateDirectory: directory)
            fatalError("asset drift accepted")
        } catch RuntimeError.assetRejected { }

        // Recreate the root asset, then prove a retained descriptor stays bound to
        // that inode even when the pathname is renamed and replaced.
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: root.path)
        try bytes.write(to: URL(fileURLWithPath: root.path))
        try FileManager.default.setAttributes([.posixPermissions: 0o400], ofItemAtPath: root.path)
        let freshPlan = try manager.prepare(qemu: qemu, qemuFramework: qemuFramework,
                                            kernel: kernel, initrd: initrd, root: root,
                                            data: data, privateDirectory: directory)
        let config = directory.appendingPathComponent("launch.json")
        try JSONEncoder().encode(freshPlan.configuration).write(to: config)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: config.path)
        let pinned = try RuntimePinnedLaunch.open(configurationPath: config.path)
        let oldRoot = directory.appendingPathComponent("root.opened.raw")
        try FileManager.default.moveItem(at: URL(fileURLWithPath: root.path), to: oldRoot)
        try Data("replacement".utf8).write(to: URL(fileURLWithPath: root.path))
        try FileManager.default.setAttributes([.posixPermissions: 0o400], ofItemAtPath: root.path)
        try pinned.revalidatePins()
        do {
            try pinned.requirePathBindingsUnchanged()
            fatalError("replacement pathname accepted for QEMU consumption")
        } catch RuntimeError.assetRejected { }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: oldRoot.path)
        try Data("mutated opened inode".utf8).write(to: oldRoot)
        try FileManager.default.setAttributes([.posixPermissions: 0o400], ofItemAtPath: oldRoot.path)
        do {
            try pinned.revalidatePins()
            fatalError("mutation of pinned inode accepted")
        } catch RuntimeError.assetRejected { }

        let oversized = directory.appendingPathComponent("oversized.json")
        try Data(repeating: 0x20, count: 64 * 1024 + 1).write(to: oversized)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: oversized.path)
        do {
            _ = try RuntimePinnedLaunch.open(configurationPath: oversized.path)
            fatalError("oversized launch config accepted")
        } catch RuntimeError.invalidConfiguration { }

        let wrongMode = directory.appendingPathComponent("wrong-mode.json")
        try JSONEncoder().encode(freshPlan.configuration).write(to: wrongMode)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: wrongMode.path)
        do {
            _ = try RuntimePinnedLaunch.open(configurationPath: wrongMode.path)
            fatalError("group-readable launch config accepted")
        } catch RuntimeError.invalidConfiguration { }

        let linkedConfig = directory.appendingPathComponent("linked.json")
        try FileManager.default.linkItem(at: config, to: linkedConfig)
        do {
            _ = try RuntimePinnedLaunch.open(configurationPath: linkedConfig.path)
            fatalError("hardlinked launch config accepted")
        } catch RuntimeError.assetRejected { }

        print("HB-11: frame/lease/replay/FD pin/mode-drift/hardlink/fixed topology tests passed")
    }
}
