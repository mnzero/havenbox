import CryptoKit
import Darwin
import Foundation
import HavenboxCore

@main struct RuntimeTests {
    static func main() async throws {
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

        try await fileBoundaryTests()
        print("HB-11/MVP-02: runtime and host file-boundary tests passed")
    }

    static func fileBoundaryTests() async throws {
        let root = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".mvp02-" + UUID().uuidString)
        let workspace = root.appendingPathComponent("workspace")
        let sources = root.appendingPathComponent("sources")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        try FileManager.default.createDirectory(at: sources, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        let store = try WorkspaceStore(directory: workspace)
        try await store.createSession(id: "session_runtime", title: "Runtime",
                                      recipeRevision: nil, toolPolicyRevision: "tools-v1")
        let turnID = try await store.sendMessage(
            sessionID: "session_runtime", commandID: "command_runtime", body: "process CSV"
        )
        let planDigest = String(repeating: "a", count: 64)
        try await store.savePlan(id: "plan_runtime", revision: 1, turnID: turnID,
                                 canonicalJSON: "{}", digest: planDigest)
        let taskID = try await store.approveExecution(
            commandID: "command_approve", turnID: turnID, planID: "plan_runtime", revision: 1,
            digest: planDigest, snapshotJSON: "{}", expiresAt: Int64.max
        )
        let boundary = try HostFileBoundary(root: workspace, store: store,
                                            maximumInputBytes: 32,
                                            maximumOutputBytes: 32, chunkBytes: 4)
        let source = sources.appendingPathComponent("input.csv")
        let input = Data("a,b\n1,2\n".utf8)
        try input.write(to: source)
        let version = try await boundary.importCSV(from: source)
        precondition(version.size == input.count)
        precondition(version.sha256 == digest(input))
        let storedVersion = try await boundary.persistedVersion(fileID: version.fileID)
        precondition(storedVersion == version)
        let storedInput = workspace.appendingPathComponent("files/input/\(version.fileID)")
        let inputAttributes = try FileManager.default.attributesOfItem(atPath: storedInput.path)
        let inputMode = inputAttributes[.posixPermissions] as! NSNumber
        precondition(inputMode.intValue & 0o222 == 0)

        let begin = try await boundary.beginInputFrame(for: version, epoch: 7, request: UUID())
        precondition(begin.kind == .beginInput && begin.header.channel == .file)
        var offset: UInt64 = 0
        var transferred = Data()
        while offset < version.size {
            let chunk = try await boundary.inputChunk(
                for: version, offset: offset, epoch: 7, request: UUID()
            )
            let object = try JSONSerialization.jsonObject(with: chunk.frame.payload) as! [String: Any]
            precondition(object["type"] as? String == "FileChunk")
            transferred.append(Data(base64Encoded: object["bytes"] as! String)!)
            offset = chunk.nextOffset
            precondition(chunk.frame.payload.count <= RuntimeChannel.file.payloadLimit)
        }
        precondition(transferred == input)
        let commit = try await boundary.commitInputFrame(for: version, epoch: 7, request: UUID())
        precondition(commit.kind == .commitInput)

        let disguised = sources.appendingPathComponent("disguised.csv")
        try Data("ordinary text\n".utf8).write(to: disguised)
        do {
            _ = try await boundary.importCSV(from: disguised)
            fatalError("plain text disguised as CSV accepted")
        } catch FileBoundaryError.invalidText { }

        let inconsistent = sources.appendingPathComponent("inconsistent.csv")
        try Data("a,b\n1,2,3\n".utf8).write(to: inconsistent)
        do {
            _ = try await boundary.importCSV(from: inconsistent)
            fatalError("inconsistent CSV rows accepted")
        } catch FileBoundaryError.invalidText { }

        let oversized = sources.appendingPathComponent("oversized.csv")
        try Data(repeating: 0x61, count: 33).write(to: oversized)
        do {
            _ = try await boundary.importCSV(from: oversized)
            fatalError("oversized input accepted")
        } catch RuntimeError.assetRejected { }

        let directory = sources.appendingPathComponent("directory.csv")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        do {
            _ = try await boundary.importCSV(from: directory)
            fatalError("directory input accepted")
        } catch RuntimeError.assetRejected { }

        let linked = sources.appendingPathComponent("linked.csv")
        try FileManager.default.createSymbolicLink(at: linked, withDestinationURL: source)
        do {
            _ = try await boundary.importCSV(from: linked)
            fatalError("symbolic-link input accepted")
        } catch RuntimeError.assetRejected { }

        let hardlinked = sources.appendingPathComponent("hardlinked.csv")
        try FileManager.default.linkItem(at: source, to: hardlinked)
        do {
            _ = try await boundary.importCSV(from: hardlinked)
            fatalError("hard-link input accepted")
        } catch RuntimeError.assetRejected { }
        try FileManager.default.removeItem(at: hardlinked)

        let fifo = sources.appendingPathComponent("special.csv")
        precondition(mkfifo(fifo.path, 0o600) == 0)
        do {
            _ = try await boundary.importCSV(from: fifo)
            fatalError("special input accepted")
        } catch RuntimeError.assetRejected { }

        do {
            _ = try RuntimeOpenedFile.open(
                path: sources.appendingPathComponent("../sources/input.csv").path,
                requirement: .importedInput(maximumBytes: 32)
            )
            fatalError("path traversal accepted")
        } catch RuntimeError.assetRejected { }

        let pinned = try RuntimeOpenedFile.open(
            path: source.path, requirement: .importedInput(maximumBytes: 32)
        )
        let moved = sources.appendingPathComponent("moved.csv")
        try FileManager.default.moveItem(at: source, to: moved)
        try input.write(to: source)
        do {
            try pinned.requirePathBindingUnchanged()
            fatalError("concurrent path replacement accepted")
        } catch RuntimeError.assetRejected { }

        let output = Data("x,y\n3,4\n".utf8)
        let outputID = "file_output"
        do {
            try await boundary.beginOutput(fileID: "file_missing-task", taskID: "task_missing",
                                           mediaType: .csv, size: UInt64(output.count),
                                           sha256: digest(output))
            fatalError("missing task accepted")
        } catch FileBoundaryError.unavailable { }
        let expiredTaskID = try await store.approveExecution(
            commandID: "command_expired", turnID: turnID, planID: "plan_runtime", revision: 1,
            digest: planDigest, snapshotJSON: "{}", expiresAt: 0
        )
        do {
            try await boundary.beginOutput(fileID: "file_expired-task", taskID: expiredTaskID,
                                           mediaType: .csv, size: UInt64(output.count),
                                           sha256: digest(output))
            fatalError("expired task approval accepted")
        } catch FileBoundaryError.unavailable { }
        try await boundary.beginOutput(fileID: outputID, taskID: taskID, mediaType: .csv,
                                       size: UInt64(output.count), sha256: digest(output))
        do {
            try await boundary.receiveOutputChunk(fileID: outputID, offset: 1,
                                                  bytes: output.prefix(4))
            fatalError("out-of-order output chunk accepted")
        } catch FileBoundaryError.transferOrder { }
        try await boundary.receiveOutputChunk(fileID: outputID, offset: 0, bytes: output.prefix(4))
        try await boundary.receiveOutputChunk(fileID: outputID, offset: 4, bytes: output.dropFirst(4))
        let committed = try await boundary.commitOutput(fileID: outputID)
        precondition(committed.sha256 == digest(output))
        let storedOutput = try await boundary.persistedVersion(fileID: outputID)
        precondition(storedOutput == committed)
        let artifact = workspace.appendingPathComponent("files/artifacts/file_output.csv")
        let artifactContents = try Data(contentsOf: artifact)
        precondition(artifactContents == output)
        let artifactAttributes = try FileManager.default.attributesOfItem(atPath: artifact.path)
        let artifactMode = artifactAttributes[.posixPermissions] as! NSNumber
        precondition(artifactMode.intValue & 0o222 == 0)

        do {
            try await boundary.beginOutput(fileID: outputID, taskID: taskID, mediaType: .csv,
                                           size: UInt64(output.count), sha256: digest(output))
            fatalError("duplicate published file ID accepted")
        } catch FileBoundaryError.unavailable { }
        let retainedBytes = try Data(contentsOf: artifact)
        let retainedVersion = try await boundary.persistedVersion(fileID: outputID)
        precondition(retainedBytes == output)
        precondition(retainedVersion == committed)

        let badID = "file_bad-hash"
        try await boundary.beginOutput(fileID: badID, taskID: taskID, mediaType: .markdown,
                                       size: UInt64(output.count), sha256: String(repeating: "0", count: 64))
        try await boundary.receiveOutputChunk(fileID: badID, offset: 0, bytes: output.prefix(4))
        try await boundary.receiveOutputChunk(fileID: badID, offset: 4, bytes: output.dropFirst(4))
        do {
            _ = try await boundary.commitOutput(fileID: badID)
            fatalError("output hash mismatch accepted")
        } catch RuntimeError.assetRejected { }
        let rejectedOutput = try await boundary.persistedVersion(fileID: badID)
        precondition(rejectedOutput == nil)

        let plainMarkdown = Data("ordinary text\n".utf8)
        try await boundary.beginOutput(fileID: "file_plain-markdown", taskID: taskID,
                                       mediaType: .markdown, size: UInt64(plainMarkdown.count),
                                       sha256: digest(plainMarkdown))
        var markdownOffset = 0
        while markdownOffset < plainMarkdown.count {
            let end = min(markdownOffset + 4, plainMarkdown.count)
            try await boundary.receiveOutputChunk(
                fileID: "file_plain-markdown", offset: UInt64(markdownOffset),
                bytes: plainMarkdown[markdownOffset..<end]
            )
            markdownOffset = end
        }
        do {
            _ = try await boundary.commitOutput(fileID: "file_plain-markdown")
            fatalError("unstructured Markdown accepted")
        } catch FileBoundaryError.invalidText { }

        do {
            try await boundary.beginOutput(
                fileID: "file_too-large", taskID: taskID, mediaType: .csv,
                size: 33, sha256: String(repeating: "0", count: 64)
            )
            fatalError("oversized output accepted")
        } catch FileBoundaryError.sizeLimit { }

        do {
            _ = try HostFileBoundary(
                root: workspace, store: store, maximumInputBytes: 32,
                maximumOutputBytes: 32,
                chunkBytes: HostFileBoundary.maximumFileChunkBytes + 1
            )
            fatalError("raw chunk limit exceeding encoded frame capacity accepted")
        } catch FileBoundaryError.sizeLimit { }

        let chunkRoot = root.appendingPathComponent("chunk-workspace")
        let chunkStore = try WorkspaceStore(directory: chunkRoot)
        let maximumChunk = HostFileBoundary.maximumFileChunkBytes
        let chunkBoundary = try HostFileBoundary(
            root: chunkRoot, store: chunkStore, maximumInputBytes: maximumChunk + 16,
            maximumOutputBytes: 32, chunkBytes: maximumChunk
        )
        let chunkSource = sources.appendingPathComponent("chunk-boundary.csv")
        var chunkInput = Data("a,b\n".utf8)
        chunkInput.append(Data(repeating: 0x31, count: maximumChunk - chunkInput.count - 3))
        chunkInput.append(Data(",2\n".utf8))
        try chunkInput.write(to: chunkSource)
        let chunkVersion = try await chunkBoundary.importCSV(from: chunkSource)
        let maximumFrameChunk = try await chunkBoundary.inputChunk(
            for: chunkVersion, offset: 0, epoch: 7, request: UUID()
        )
        let maximumPayload = try JSONSerialization.jsonObject(
            with: maximumFrameChunk.frame.payload
        ) as! [String: Any]
        precondition(Data(base64Encoded: maximumPayload["bytes"] as! String)!.count == maximumChunk)
        precondition(maximumFrameChunk.frame.payload.count <= RuntimeChannel.file.payloadLimit)

        let staging = workspace.appendingPathComponent("files/staging")
        let originalStaging = workspace.appendingPathComponent("files/staging-original")
        try FileManager.default.moveItem(at: staging, to: originalStaging)
        try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: false)
        do {
            try await boundary.beginOutput(fileID: "file_replaced-directory", taskID: taskID,
                                           mediaType: .csv, size: UInt64(output.count),
                                           sha256: digest(output))
            fatalError("replaced staging directory accepted")
        } catch FileBoundaryError.invalidPath { }
        try FileManager.default.removeItem(at: staging)
        try FileManager.default.moveItem(at: originalStaging, to: staging)

        let revokedID = "file_revoked-before-commit"
        try await boundary.beginOutput(fileID: revokedID, taskID: taskID, mediaType: .csv,
                                       size: UInt64(output.count), sha256: digest(output))
        try await boundary.receiveOutputChunk(fileID: revokedID, offset: 0, bytes: output.prefix(4))
        try await boundary.receiveOutputChunk(fileID: revokedID, offset: 4, bytes: output.dropFirst(4))
        try await store.markInterruptedAfterProcessVerification()
        do {
            _ = try await boundary.commitOutput(fileID: revokedID)
            fatalError("commit accepted a task that was no longer writable")
        } catch FileBoundaryError.unavailable { }
        precondition(!FileManager.default.fileExists(
            atPath: workspace.appendingPathComponent("files/artifacts/\(revokedID).csv").path
        ))
        let revokedVersion = try await boundary.persistedVersion(fileID: revokedID)
        precondition(revokedVersion == nil)
    }

    static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}
