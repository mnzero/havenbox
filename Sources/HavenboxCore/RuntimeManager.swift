import Darwin
import Foundation

public struct RuntimeAsset: Codable, Equatable, Sendable {
    public let path: String
    public let sha256: String
    public init(path: String, sha256: String) { self.path = path; self.sha256 = sha256 }
}

public struct RuntimeLaunchConfiguration: Codable, Equatable, Sendable {
    public let qemu: RuntimeAsset
    public let qemuFramework: RuntimeAsset
    public let kernel: RuntimeAsset
    public let initrd: RuntimeAsset
    public let root: RuntimeAsset
    public let dataPath: String
    public let privateDirectory: String
    public let instance: UUID
    public let epoch: UInt64
    public let role: String
    public let uiFIFO: String
}

/// This manager only starts the fixed HB-11 synthetic validation guest.
/// Product task admission and ValidationRunGrant issuance remain unavailable.
public struct RuntimeManager {
    public typealias Asset = RuntimeAsset

    public struct Prepared: Sendable {
        public let executable: URL
        public let arguments: [String]
        public let configuration: RuntimeLaunchConfiguration
    }

    public init() {}

    public func verify(_ asset: Asset, executable: Bool = false) throws -> String {
        _ = try RuntimeOpenedFile.open(
            path: asset.path,
            requirement: .immutable(sha256: asset.sha256, executable: executable)
        )
        return asset.path
    }

    /// Reuses the reviewed HB-02 virt/HVF/disk topology, with immutable guest init
    /// and three dedicated virtio-serial channels instead of an executable fw_cfg payload.
    public func prepare(qemu: Asset, qemuFramework: Asset, kernel: Asset, initrd: Asset, root: Asset,
                        data: URL, privateDirectory: URL, instance: UUID = UUID(),
                        epoch: UInt64 = 1, role: String = "harness") throws -> Prepared {
        guard instance != RuntimeFrame.zeroUUID, epoch > 0, ["harness", "worker"].contains(role) else {
            throw RuntimeError.assetRejected("invalid instance, epoch, or synthetic role")
        }
        let qemuFile = try RuntimeOpenedFile.open(
            path: qemu.path, requirement: .immutable(sha256: qemu.sha256, executable: true)
        )
        let qemuFrameworkFile = try RuntimeOpenedFile.open(
            path: qemuFramework.path,
            requirement: .immutable(sha256: qemuFramework.sha256, executable: true)
        )
        let kernelFile = try RuntimeOpenedFile.open(
            path: kernel.path, requirement: .immutable(sha256: kernel.sha256, executable: false)
        )
        let initrdFile = try RuntimeOpenedFile.open(
            path: initrd.path, requirement: .immutable(sha256: initrd.sha256, executable: false)
        )
        let rootFile = try RuntimeOpenedFile.open(
            path: root.path, requirement: .immutable(sha256: root.sha256, executable: false)
        )
        let dataFile = try RuntimeOpenedFile.open(path: data.path, requirement: .writableData)
        let directory = privateDirectory.standardizedFileURL
        let attributes = try FileManager.default.attributesOfItem(atPath: directory.path)
        guard directory.path == privateDirectory.path,
              directory.resolvingSymlinksInPath().path == directory.path else {
            throw RuntimeError.assetRejected("private runtime directory must be canonical")
        }
        guard attributes[.type] as? FileAttributeType == .typeDirectory,
              (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid() else {
            throw RuntimeError.assetRejected("private runtime directory owner or mode rejected")
        }
        guard data.path == data.standardizedFileURL.path else {
            throw RuntimeError.assetRejected("data path must be canonical")
        }
        let readOnly = [qemuFile, qemuFrameworkFile, kernelFile, initrdFile, rootFile]
        let identities = readOnly.map(\.identity)
        guard Set(identities).count == identities.count,
              !Set(identities).contains(dataFile.identity) else {
            throw RuntimeError.assetRejected("runtime assets must have distinct identities")
        }
        let executable = qemu.path
        let kernelPath = kernel.path
        let initrdPath = initrd.path
        let rootPath = root.path
        let instanceHex = instance.uuidBytes.map { String(format: "%02x", $0) }.joined()
        var args = ["-M", "virt", "-accel", "hvf", "-cpu", "host", "-smp", "2", "-m", "4096M",
                    "-nodefaults", "-nographic", "-nic", "none", "-serial", "null", "-monitor", "none",
                    "-kernel", kernelPath, "-initrd", initrdPath,
                    "-append", "console=ttyAMA0 root=/dev/vda ro init=/sbin/hb-init hb.instance=\(instanceHex) hb.epoch=\(epoch) hb.synthetic_role=\(role)",
                    "-drive", "if=virtio,format=raw,file=\(rootPath),readonly=on",
                    "-drive", "if=virtio,format=raw,file=\(data.path),snapshot=off",
                    "-device", "virtio-serial-pci,id=hbserial"]
        for channel in ["control", "file", "inference"] {
            let socket = directory.appendingPathComponent(channel + ".sock").path
            guard !socket.contains(","), socket.utf8.count < 100,
                  !FileManager.default.fileExists(atPath: socket) else {
                throw RuntimeError.assetRejected("runtime channel socket path rejected")
            }
            args += ["-chardev", "socket,id=\(channel),path=\(socket),server=on,wait=off",
                     "-device", "virtserialport,bus=hbserial.0,chardev=\(channel),name=hb.\(channel)"]
        }
        let configuration = RuntimeLaunchConfiguration(
            qemu: qemu, qemuFramework: qemuFramework, kernel: kernel, initrd: initrd,
            root: root, dataPath: data.path,
            privateDirectory: directory.path, instance: instance, epoch: epoch, role: role,
            uiFIFO: directory.appendingPathComponent("ui-control.fifo").path
        )
        return Prepared(executable: URL(fileURLWithPath: executable), arguments: args,
                        configuration: configuration)
    }

    public func start(_ prepared: Prepared, helper: Asset) throws -> RuntimeHandle {
        let helperPath = try verify(helper, executable: true)
        let directory = URL(fileURLWithPath: prepared.configuration.privateDirectory, isDirectory: true)
        let config = directory.appendingPathComponent("launch.json")
        let plist = directory.appendingPathComponent("launchd.plist")
        for path in [config.path, plist.path, prepared.configuration.uiFIFO] {
            guard !FileManager.default.fileExists(atPath: path) else { throw RuntimeError.invalidConfiguration }
        }
        let label = "dev.havenbox.runtime.\(prepared.configuration.instance.uuidString.lowercased())"
        let domain = "gui/\(getuid())"
        var created: [String] = []
        do {
            try JSONEncoder().encode(prepared.configuration).write(to: config, options: .withoutOverwriting)
            created.append(config.path)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: config.path)
            guard mkfifo(prepared.configuration.uiFIFO, 0o600) == 0 else { throw RuntimeError.system(errno) }
            created.append(prepared.configuration.uiFIFO)
            let object: [String: Any] = [
                "Label": label,
                "ProgramArguments": [helperPath, "--run", config.path],
                "RunAtLoad": true,
                "AbandonProcessGroup": false,
                "ProcessType": "Interactive",
                "StandardOutPath": directory.appendingPathComponent("helper.stdout").path,
                "StandardErrorPath": directory.appendingPathComponent("helper.stderr").path,
            ]
            let plistData = try PropertyListSerialization.data(fromPropertyList: object, format: .xml, options: 0)
            try plistData.write(to: plist, options: .withoutOverwriting)
            created.append(plist.path)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: plist.path)
            try runLaunchctl(["bootstrap", domain, plist.path])
            let descriptor = try openFIFOForWriting(prepared.configuration.uiFIFO, timeout: 5)
            let handle = RuntimeHandle(label: label, domain: domain, descriptor: descriptor,
                                       configuration: prepared.configuration,
                                       cleanupPaths: [
                                           plist.path, config.path, prepared.configuration.uiFIFO,
                                           directory.appendingPathComponent("control.sock").path,
                                           directory.appendingPathComponent("file.sock").path,
                                           directory.appendingPathComponent("inference.sock").path,
                                       ])
            try handle.hello()
            return handle
        } catch {
            try? runLaunchctl(["bootout", domain, plist.path])
            for path in created.reversed() { try? FileManager.default.removeItem(atPath: path) }
            throw error
        }
    }

    private func openFIFOForWriting(_ path: String, timeout: TimeInterval) throws -> Int32 {
        let deadline = DispatchTime.now().uptimeNanoseconds + UInt64(timeout * 1_000_000_000)
        repeat {
            let descriptor = Darwin.open(path, O_WRONLY | O_NONBLOCK | O_CLOEXEC)
            if descriptor >= 0 {
                guard fcntl(descriptor, F_SETNOSIGPIPE, 1) == 0 else {
                    let saved = errno
                    Darwin.close(descriptor)
                    throw RuntimeError.system(saved)
                }
                return descriptor
            }
            if errno != ENXIO && errno != ENOENT { throw RuntimeError.system(errno) }
            usleep(20_000)
        } while DispatchTime.now().uptimeNanoseconds < deadline
        throw RuntimeError.unavailable("helper did not open UI control FIFO")
    }

    private func runLaunchctl(_ arguments: [String]) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw RuntimeError.launchd(process.terminationStatus) }
    }
}

/// Helper-owned launch state. Open descriptors pin the objects that were checked,
/// but pathname-based QEMU executable/kernel/initrd loading remains unverified.
public final class RuntimePinnedLaunch {
    public let configuration: RuntimeLaunchConfiguration
    public let executablePath: String
    public let arguments: [String]
    private let config: RuntimeOpenedFile
    private let qemu: RuntimeOpenedFile
    private let qemuFramework: RuntimeOpenedFile
    private let kernel: RuntimeOpenedFile
    private let initrd: RuntimeOpenedFile
    private let root: RuntimeOpenedFile
    private let data: RuntimeOpenedFile

    public static func open(configurationPath: String) throws -> RuntimePinnedLaunch {
        let config = try RuntimeOpenedFile.open(
            path: configurationPath, requirement: .configuration(maximumBytes: 64 * 1024)
        )
        let configuration = try JSONDecoder().decode(
            RuntimeLaunchConfiguration.self, from: config.readAll(maximumBytes: 64 * 1024)
        )
        let configURL = URL(fileURLWithPath: configurationPath)
        let directory = URL(fileURLWithPath: configuration.privateDirectory, isDirectory: true)
        guard configURL.deletingLastPathComponent().path == directory.path,
              configuration.uiFIFO == directory.appendingPathComponent("ui-control.fifo").path else {
            throw RuntimeError.invalidConfiguration
        }
        let prepared = try RuntimeManager().prepare(
            qemu: configuration.qemu, qemuFramework: configuration.qemuFramework,
            kernel: configuration.kernel, initrd: configuration.initrd,
            root: configuration.root, data: URL(fileURLWithPath: configuration.dataPath),
            privateDirectory: directory, instance: configuration.instance,
            epoch: configuration.epoch, role: configuration.role
        )
        let qemu = try RuntimeOpenedFile.open(
            path: configuration.qemu.path,
            requirement: .immutable(sha256: configuration.qemu.sha256, executable: true)
        )
        let qemuFramework = try RuntimeOpenedFile.open(
            path: configuration.qemuFramework.path,
            requirement: .immutable(sha256: configuration.qemuFramework.sha256, executable: true)
        )
        let kernel = try RuntimeOpenedFile.open(
            path: configuration.kernel.path,
            requirement: .immutable(sha256: configuration.kernel.sha256, executable: false)
        )
        let initrd = try RuntimeOpenedFile.open(
            path: configuration.initrd.path,
            requirement: .immutable(sha256: configuration.initrd.sha256, executable: false)
        )
        let root = try RuntimeOpenedFile.open(
            path: configuration.root.path,
            requirement: .immutable(sha256: configuration.root.sha256, executable: false)
        )
        let data = try RuntimeOpenedFile.open(path: configuration.dataPath, requirement: .writableData)
        let identities = [qemu.identity, qemuFramework.identity, kernel.identity, initrd.identity, root.identity]
        guard Set(identities).count == identities.count, !Set(identities).contains(data.identity) else {
            throw RuntimeError.invalidConfiguration
        }
        return RuntimePinnedLaunch(
            configuration: configuration, executablePath: prepared.executable.path,
            arguments: prepared.arguments, config: config, qemu: qemu,
            qemuFramework: qemuFramework, kernel: kernel,
            initrd: initrd, root: root, data: data
        )
    }

    private init(configuration: RuntimeLaunchConfiguration, executablePath: String,
                 arguments: [String], config: RuntimeOpenedFile, qemu: RuntimeOpenedFile,
                 qemuFramework: RuntimeOpenedFile,
                 kernel: RuntimeOpenedFile, initrd: RuntimeOpenedFile,
                 root: RuntimeOpenedFile, data: RuntimeOpenedFile) {
        self.configuration = configuration
        self.executablePath = executablePath
        self.arguments = arguments
        self.config = config
        self.qemu = qemu
        self.qemuFramework = qemuFramework
        self.kernel = kernel
        self.initrd = initrd
        self.root = root
        self.data = data
    }

    public func revalidatePins() throws {
        try config.requireUnchanged()
        guard try qemu.sha256() == configuration.qemu.sha256,
              try qemuFramework.sha256() == configuration.qemuFramework.sha256,
              try kernel.sha256() == configuration.kernel.sha256,
              try initrd.sha256() == configuration.initrd.sha256,
              try root.sha256() == configuration.root.sha256 else {
            throw RuntimeError.assetRejected("pinned asset changed")
        }
        try data.requireUnchanged()
    }

    /// Confirms that each pathname still names the pinned inode. The check narrows
    /// replacement races but does not turn pathname-based QEMU loading into FD binding.
    public func requirePathBindingsUnchanged() throws {
        let currentQEMU = try RuntimeOpenedFile.open(
            path: configuration.qemu.path,
            requirement: .immutable(sha256: configuration.qemu.sha256, executable: true)
        )
        let currentQEMUFramework = try RuntimeOpenedFile.open(
            path: configuration.qemuFramework.path,
            requirement: .immutable(sha256: configuration.qemuFramework.sha256, executable: true)
        )
        let currentKernel = try RuntimeOpenedFile.open(
            path: configuration.kernel.path,
            requirement: .immutable(sha256: configuration.kernel.sha256, executable: false)
        )
        let currentInitrd = try RuntimeOpenedFile.open(
            path: configuration.initrd.path,
            requirement: .immutable(sha256: configuration.initrd.sha256, executable: false)
        )
        let currentRoot = try RuntimeOpenedFile.open(
            path: configuration.root.path,
            requirement: .immutable(sha256: configuration.root.sha256, executable: false)
        )
        let currentData = try RuntimeOpenedFile.open(
            path: configuration.dataPath, requirement: .writableData
        )
        guard currentQEMU.identity == qemu.identity,
              currentQEMUFramework.identity == qemuFramework.identity,
              currentKernel.identity == kernel.identity,
              currentInitrd.identity == initrd.identity,
              currentRoot.identity == root.identity,
              currentData.identity == data.identity else {
            throw RuntimeError.assetRejected("asset pathname identity changed")
        }
    }

    public func spawnQEMU() throws -> pid_t {
        try revalidatePins()
        try requirePathBindingsUnchanged()
        var attributes: posix_spawnattr_t?
        var actions: posix_spawn_file_actions_t?
        guard posix_spawnattr_init(&attributes) == 0,
              posix_spawn_file_actions_init(&actions) == 0 else {
            throw RuntimeError.invalidConfiguration
        }
        defer {
            posix_spawnattr_destroy(&attributes)
            posix_spawn_file_actions_destroy(&actions)
        }
        guard posix_spawn_file_actions_addopen(
            &actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0
        ) == 0 else { throw RuntimeError.invalidConfiguration }
        let strings = ([executablePath] + arguments).compactMap { strdup($0) }
        guard strings.count == arguments.count + 1,
              let pathEnvironment = strdup("PATH=/usr/bin:/bin"),
              let frameworkEnvironment = strdup(
                  "HB02_QEMU_SYSTEM_FRAMEWORK=\(configuration.qemuFramework.path)"
              ) else {
            strings.forEach { free($0) }
            throw RuntimeError.invalidConfiguration
        }
        let environment: [UnsafeMutablePointer<CChar>?] = [
            pathEnvironment, frameworkEnvironment, nil,
        ]
        defer {
            strings.forEach { free($0) }
            environment.compactMap { $0 }.forEach { free($0) }
        }
        var argv: [UnsafeMutablePointer<CChar>?] = strings + [nil]
        var envp = environment
        var pid: pid_t = 0
        let result = posix_spawn(&pid, executablePath, &actions, &attributes, &argv, &envp)
        guard result == 0, pid > 1, getpgid(pid) == getpgrp() else {
            if pid > 1 { _ = kill(pid, SIGKILL) }
            throw RuntimeError.system(result == 0 ? errno : result)
        }
        return pid
    }
}

public final class RuntimeHandle {
    public let configuration: RuntimeLaunchConfiguration
    private let label: String
    private let domain: String
    private var descriptor: Int32
    private var sequence: UInt64 = 0
    private let cleanupPaths: [String]

    fileprivate init(label: String, domain: String, descriptor: Int32,
                     configuration: RuntimeLaunchConfiguration, cleanupPaths: [String]) {
        self.label = label; self.domain = domain; self.descriptor = descriptor
        self.configuration = configuration; self.cleanupPaths = cleanupPaths
    }

    fileprivate func hello() throws {
        try write("HELLO \(configuration.instance.uuidString.lowercased()) \(configuration.epoch)\n")
    }

    public func heartbeat() throws {
        guard sequence < UInt64.max else { throw RuntimeError.invalidControl }
        sequence += 1
        try write("BEAT \(sequence)\n")
    }

    public func terminate() throws {
        if descriptor >= 0 {
            try? write("STOP\n")
            Darwin.close(descriptor)
            descriptor = -1
        }
        let socket = configuration.privateDirectory + "/control.sock"
        let deadline = DispatchTime.now().uptimeNanoseconds + 4_000_000_000
        while FileManager.default.fileExists(atPath: socket),
              DispatchTime.now().uptimeNanoseconds < deadline { usleep(20_000) }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["bootout", "\(domain)/\(label)"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        guard [0, 3, 113].contains(process.terminationStatus) else {
            throw RuntimeError.launchd(process.terminationStatus)
        }
        for path in cleanupPaths { try? FileManager.default.removeItem(atPath: path) }
    }

    deinit { if descriptor >= 0 { Darwin.close(descriptor) } }

    private func write(_ text: String) throws {
        let bytes = Array(text.utf8)
        let written = bytes.withUnsafeBytes { Darwin.write(descriptor, $0.baseAddress, bytes.count) }
        guard written == bytes.count else { throw RuntimeError.system(errno) }
    }
}

extension UUID {
    fileprivate var uuidBytes: [UInt8] {
        var value = uuid
        return withUnsafeBytes(of: &value) { Array($0) }
    }
}
