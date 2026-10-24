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
        func asset(_ name: String) throws -> RuntimeManager.Asset {
            let url = directory.appendingPathComponent(name)
            try bytes.write(to: url)
            return RuntimeManager.Asset(url: url, sha256: digest)
        }
        let qemu = try asset("qemu"), kernel = try asset("Image"), initrd = try asset("initrd"), root = try asset("root.raw")
        let data = try asset("data.raw").url
        let manager = RuntimeManager()
        let plan = try manager.prepare(qemu: qemu, kernel: kernel, initrd: initrd, root: root,
                                       data: data, privateDirectory: directory)
        precondition(plan.arguments.contains("none") && !plan.arguments.contains("-netdev"))
        precondition(plan.arguments.filter { $0.contains("name=hb.") }.count == 3)
        // A distinct pathname can alias any read-only asset's actual inode.
        for (index, readOnly) in [qemu, kernel, initrd, root].enumerated() {
            let alias = directory.appendingPathComponent("hardlink-\(index).raw")
            try FileManager.default.linkItem(at: readOnly.url, to: alias)
            do {
                _ = try manager.prepare(qemu: qemu, kernel: kernel, initrd: initrd, root: root,
                                        data: alias, privateDirectory: directory)
                fatalError("hardlinked data accepted for \(readOnly.url.lastPathComponent)")
            } catch RuntimeError.invalidConfiguration { }
        }
        do { try manager.start(plan) } catch RuntimeError.unavailable { } // Must not execute synthetic qemu.
        try Data("changed".utf8).write(to: root.url)
        do {
            _ = try manager.prepare(qemu: qemu, kernel: kernel, initrd: initrd, root: root,
                                    data: data, privateDirectory: directory)
            fatalError("asset drift accepted")
        } catch RuntimeError.assetRejected { }
        print("HB-11: lease/handshake/replay/asset drift/hardlink/fixed topology tests passed; VM isolation unverified")
    }
}
