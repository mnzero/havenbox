import Darwin
import Foundation
import HavenboxCore

private let arguments = Array(CommandLine.arguments.dropFirst())
if arguments == ["--version"] {
    print("havenbox-helper 0.1.0-candidate · hb.channel.v\(ProtocolHeader.version)")
    exit(0)
}
guard arguments.count == 2, arguments[0] == "--run" else {
    FileHandle.standardError.write(Data("HB-11 helper requires a fixed --run configuration; no process started.\n".utf8))
    exit(78)
}

do {
    try Helper(configurationPath: arguments[1]).run()
} catch {
    FileHandle.standardError.write(Data("HB-11 helper failed: \(error)\n".utf8))
    exit(78)
}

private final class Helper {
    private let configuration: RuntimeLaunchConfiguration
    private let launch: RuntimePinnedLaunch
    private let fifo: Int32
    private var fifoBuffer = Data()
    private var uiConnected = false
    private var stopRequested = false
    private var watchdog: RuntimeWatchdog
    private var guestRequests: RuntimeRequestRegistry
    private var qemuPID: pid_t?
    private var qemuReaped = false

    init(configurationPath: String) throws {
        launch = try RuntimePinnedLaunch.open(configurationPath: configurationPath)
        configuration = launch.configuration
        var status = stat()
        guard lstat(configuration.uiFIFO, &status) == 0,
              (status.st_mode & S_IFMT) == S_IFIFO,
              (status.st_mode & 0o777) == 0o600,
              status.st_uid == getuid() else { throw RuntimeError.invalidConfiguration }
        fifo = Darwin.open(configuration.uiFIFO, O_RDONLY | O_NONBLOCK | O_CLOEXEC)
        guard fifo >= 0 else { throw RuntimeError.system(errno) }
        guard fstat(fifo, &status) == 0,
              status.st_mode & S_IFMT == S_IFIFO,
              status.st_mode & 0o777 == 0o600,
              status.st_uid == getuid() else {
            Darwin.close(fifo)
            throw RuntimeError.invalidConfiguration
        }
        let now = DispatchTime.now().uptimeNanoseconds
        watchdog = try RuntimeWatchdog(instance: configuration.instance, epoch: configuration.epoch,
                                       now: now, ttl: 5_000_000_000)
        guestRequests = try RuntimeRequestRegistry()
    }

    deinit { Darwin.close(fifo); stopQEMU() }

    func run() throws {
        defer {
            stopQEMU()
            cleanupRuntimeArtifacts()
            bootoutOwnJob()
        }
        try spawnQEMU()
        let channels = try waitAndConnectChannels(timeout: 30)
        defer { channels.forEach { Darwin.close($0) } }
        try guestHandshake(control: channels[0])
        try supervise(control: channels[0])
    }

    private func cleanupRuntimeArtifacts() {
        for name in [
            "control.sock", "file.sock", "inference.sock", "ui-control.fifo",
            "launch.json", "launchd.plist",
        ] {
            _ = unlink(configuration.privateDirectory + "/" + name)
        }
    }

    private func bootoutOwnJob() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = [
            "bootout", "gui/\(getuid())/dev.havenbox.runtime.\(configuration.instance.uuidString.lowercased())",
        ]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return }
        process.waitUntilExit()
    }

    private func waitAndConnectChannels(timeout: TimeInterval) throws -> [Int32] {
        let deadline = DispatchTime.now().uptimeNanoseconds + UInt64(timeout * 1_000_000_000)
        let names = ["control.sock", "file.sock", "inference.sock"]
        while DispatchTime.now().uptimeNanoseconds < deadline {
            try pollUI()
            guard qemuRunning() else { throw RuntimeError.unavailable("QEMU exited during boot") }
            let paths = names.map { configuration.privateDirectory + "/" + $0 }
            if paths.allSatisfy({ isSocket($0) }) {
                return try paths.map(connectUnix)
            }
            usleep(20_000)
        }
        throw RuntimeError.unavailable("QEMU channels did not appear")
    }

    private func guestHandshake(control: Int32) throws {
        let request = UUID()
        let payload = Data(#"{"type":"Hello","instance":"\#(configuration.instance.hex)"}"#.utf8)
        let frame = try RuntimeFrame(channel: .control, epoch: configuration.epoch,
                                     request: request, payload: payload, direction: .hostToGuest)
        try guestRequests.registerLocal(request)
        try sendFrame(frame.data, to: control)
        let deadline = DispatchTime.now().uptimeNanoseconds + 5_000_000_000
        var buffer = Data()
        while DispatchTime.now().uptimeNanoseconds < deadline {
            try pollUI()
            if let reply = try receiveFrame(from: control, buffer: &buffer) {
                guard reply.kind == .helloAck, reply.header.request == request,
                      try replyString(reply, "instance") == configuration.instance.hex else {
                    throw RuntimeError.invalidControl
                }
                try guestRequests.acceptRemote(reply, respondsToLocal: true)
                return
            }
            usleep(10_000)
        }
        throw RuntimeError.unavailable("guest handshake timed out")
    }

    private func supervise(control: Int32) throws {
        var guestSequence: UInt64 = 0
        var nextHeartbeat = DispatchTime.now().uptimeNanoseconds
        var controlBuffer = Data()
        while qemuRunning() {
            try pollUI()
            if stopRequested {
                try stopGuest(control: control, buffer: &controlBuffer)
                return
            }
            let now = DispatchTime.now().uptimeNanoseconds
            if now >= nextHeartbeat {
                guestSequence += 1
                let payload = Data(#"{"type":"Heartbeat","sequence":"\#(guestSequence)"}"#.utf8)
                let frame = try RuntimeFrame(channel: .control, epoch: configuration.epoch,
                                             request: UUID(), payload: payload, direction: .hostToGuest)
                try sendFrame(frame.data, to: control)
                nextHeartbeat = now + 1_000_000_000
            }
            for _ in 0..<4 {
                guard let frame = try receiveFrame(from: control, buffer: &controlBuffer) else {
                    break
                }
                guard frame.kind == .event else { throw RuntimeError.invalidControl }
                try guestRequests.acceptRemote(frame, respondsToLocal: false)
                try pollUI()
            }
            usleep(10_000)
        }
        throw RuntimeError.unavailable("QEMU exited")
    }

    private func spawnQEMU() throws {
        qemuPID = try launch.spawnQEMU()
        qemuReaped = false
    }

    private func qemuRunning() -> Bool {
        guard let pid = qemuPID, !qemuReaped else { return false }
        var status: Int32 = 0
        let result = waitpid(pid, &status, WNOHANG)
        if result == 0 { return true }
        if result == pid { qemuReaped = true }
        return false
    }

    private func stopGuest(control: Int32, buffer: inout Data) throws {
        let request = UUID()
        let frame = try RuntimeFrame(channel: .control, epoch: configuration.epoch,
                                     request: request, payload: Data(#"{"type":"Stop"}"#.utf8),
                                     direction: .hostToGuest)
        try guestRequests.registerLocal(request)
        try sendFrame(frame.data, to: control)
        let deadline = DispatchTime.now().uptimeNanoseconds + 3_000_000_000
        while DispatchTime.now().uptimeNanoseconds < deadline {
            if let response = try receiveFrame(from: control, buffer: &buffer),
               response.kind == .stopped, response.header.request == request {
                try guestRequests.acceptRemote(response, respondsToLocal: true)
                return
            }
            usleep(10_000)
        }
        throw RuntimeError.unavailable("guest stop acknowledgement timed out")
    }

    private func pollUI() throws {
        let now = DispatchTime.now().uptimeNanoseconds
        guard watchdog.tick(now: now) else { throw RuntimeError.invalidControl }
        var bytes = [UInt8](repeating: 0, count: 4096)
        let count = Darwin.read(fifo, &bytes, bytes.count)
        if count > 0 {
            fifoBuffer.append(contentsOf: bytes.prefix(count))
            while let newline = fifoBuffer.firstIndex(of: 0x0a) {
                let line = String(decoding: fifoBuffer[..<newline], as: UTF8.self)
                fifoBuffer.removeSubrange(...newline)
                try acceptUI(line, now: now)
            }
        } else if count == 0, uiConnected {
            watchdog.disconnect()
            throw RuntimeError.invalidControl
        } else if count < 0, errno != EAGAIN && errno != EWOULDBLOCK {
            throw RuntimeError.system(errno)
        }
    }

    private func acceptUI(_ line: String, now: UInt64) throws {
        let parts = line.split(separator: " ")
        if parts.count == 3, parts[0] == "HELLO",
           let instance = UUID(uuidString: String(parts[1])),
           let epoch = UInt64(parts[2]) {
            try watchdog.hello(instance: instance, epoch: epoch, now: now)
            uiConnected = true
        } else if parts.count == 2, parts[0] == "BEAT", let sequence = UInt64(parts[1]) {
            try watchdog.heartbeat(sequence: sequence, now: now)
        } else if line == "STOP" {
            stopRequested = true
        } else {
            watchdog.disconnect(reason: "invalid UI control")
            throw RuntimeError.invalidControl
        }
    }

    private func receiveFrame(from descriptor: Int32, buffer: inout Data) throws -> RuntimeFrame? {
        let maximum = ProtocolHeader.byteCount + Int(RuntimeChannel.control.payloadLimit)
        guard buffer.count <= maximum else { throw RuntimeError.invalidControl }

        var total: Int?
        var headerBytes: [UInt8]?
        if buffer.count >= ProtocolHeader.byteCount {
            let candidate = Array(buffer.prefix(ProtocolHeader.byteCount))
            let header = try ProtocolHeader(bytes: candidate, expectedEpoch: configuration.epoch)
            guard header.channel == .control else { throw RuntimeError.invalidControl }
            headerBytes = candidate
            total = ProtocolHeader.byteCount + Int(header.payloadLength)
        }
        if let total, buffer.count >= total, let headerBytes {
            let payload = Data(buffer[ProtocolHeader.byteCount..<total])
            buffer.removeSubrange(0..<total)
            return try RuntimeFrame(headerBytes: headerBytes, payload: payload,
                                    expectedEpoch: configuration.epoch, direction: .guestToHost)
        }

        let remaining = (total ?? ProtocolHeader.byteCount) - buffer.count
        guard remaining > 0 else { throw RuntimeError.invalidControl }
        var bytes = [UInt8](repeating: 0, count: min(remaining, 16_384))
        let count = Darwin.recv(descriptor, &bytes, bytes.count, MSG_DONTWAIT)
        if count > 0 { buffer.append(contentsOf: bytes.prefix(count)) }
        else if count == 0 { throw RuntimeError.invalidControl }
        else if errno != EAGAIN && errno != EWOULDBLOCK { throw RuntimeError.system(errno) }
        guard buffer.count >= ProtocolHeader.byteCount else { return nil }
        let candidate = Array(buffer.prefix(ProtocolHeader.byteCount))
        let header = try ProtocolHeader(bytes: candidate, expectedEpoch: configuration.epoch)
        guard header.channel == .control else { throw RuntimeError.invalidControl }
        let complete = ProtocolHeader.byteCount + Int(header.payloadLength)
        guard complete <= maximum else { throw RuntimeError.invalidControl }
        guard buffer.count >= complete else { return nil }
        let payload = Data(buffer[ProtocolHeader.byteCount..<complete])
        buffer.removeSubrange(0..<complete)
        return try RuntimeFrame(headerBytes: candidate, payload: payload,
                                expectedEpoch: configuration.epoch, direction: .guestToHost)
    }

    private func replyString(_ frame: RuntimeFrame, _ key: String) throws -> String? {
        let value = try JSONSerialization.jsonObject(with: frame.payload)
        return (value as? [String: Any])?[key] as? String
    }

    private func sendFrame(_ data: Data, to descriptor: Int32) throws {
        let deadline = DispatchTime.now().uptimeNanoseconds + 1_000_000_000
        var offset = 0
        try data.withUnsafeBytes { bytes in
            while offset < data.count {
                let count = Darwin.send(
                    descriptor, bytes.baseAddress! + offset, data.count - offset, MSG_DONTWAIT
                )
                if count > 0 {
                    offset += count
                } else if count < 0, errno == EAGAIN || errno == EWOULDBLOCK {
                    try pollUI()
                    guard DispatchTime.now().uptimeNanoseconds < deadline else {
                        throw RuntimeError.unavailable("guest control backpressure timeout")
                    }
                    usleep(5_000)
                } else {
                    throw RuntimeError.system(errno)
                }
            }
        }
    }

    private func stopQEMU() {
        guard let pid = qemuPID else { return }
        if !qemuReaped, getpgid(pid) == getpgrp() {
            _ = kill(pid, SIGTERM)
            let deadline = DispatchTime.now().uptimeNanoseconds + 3_000_000_000
            while qemuRunning(), DispatchTime.now().uptimeNanoseconds < deadline { usleep(20_000) }
            if qemuRunning() { _ = kill(pid, SIGKILL) }
        }
        if !qemuReaped {
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0, errno == EINTR { }
            qemuReaped = true
        }
        qemuPID = nil
    }
}

private func isSocket(_ path: String) -> Bool {
    var status = stat()
    return lstat(path, &status) == 0 && (status.st_mode & S_IFMT) == S_IFSOCK
}

private func connectUnix(_ path: String) throws -> Int32 {
    let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
    guard descriptor >= 0 else { throw RuntimeError.system(errno) }
    var noSignal: Int32 = 1
    guard setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal,
                     socklen_t(MemoryLayout<Int32>.size)) == 0 else {
        let saved = errno; Darwin.close(descriptor); throw RuntimeError.system(saved)
    }
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let encoded = Array(path.utf8) + [0]
    let capacity = MemoryLayout.size(ofValue: address.sun_path)
    guard encoded.count <= capacity else { Darwin.close(descriptor); throw RuntimeError.invalidConfiguration }
    withUnsafeMutableBytes(of: &address.sun_path) { target in target.copyBytes(from: encoded) }
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    let result = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    guard result == 0 else { let saved = errno; Darwin.close(descriptor); throw RuntimeError.system(saved) }
    return descriptor
}

private extension UUID {
    var hex: String {
        var value = uuid
        return withUnsafeBytes(of: &value) { bytes in
            bytes.map { String(format: "%02x", $0) }.joined()
        }
    }
}
