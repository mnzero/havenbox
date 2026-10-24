import CryptoKit
import Foundation

public enum RuntimeChannel: UInt8, Sendable {
    case control = 1
    case file = 2
    case inference = 3

    public var payloadLimit: UInt64 {
        switch self {
        case .control: 65_536
        case .file: 262_144
        case .inference: 4_194_304
        }
    }
}

public enum RuntimeDirection: Sendable { case hostToGuest, guestToHost }

public enum RuntimeMessageKind: String, Sendable {
    case hello = "Hello"
    case heartbeat = "Heartbeat"
    case stop = "Stop"
    case callPermit = "CallPermit"
    case helloAck = "HelloAck"
    case event = "Event"
    case stopped = "Stopped"
    case readArtifact = "ReadArtifact"
    case fileChunk = "FileChunk"
    case inferenceRequest = "InferenceRequest"
    case inferenceResponse = "InferenceResponse"
    case cancel = "Cancel"

    fileprivate func allowed(channel: RuntimeChannel, direction: RuntimeDirection) -> Bool {
        switch (channel, direction, self) {
        case (.control, .hostToGuest, .hello), (.control, .hostToGuest, .heartbeat),
             (.control, .hostToGuest, .stop), (.control, .hostToGuest, .callPermit),
             (.control, .guestToHost, .helloAck), (.control, .guestToHost, .event),
             (.control, .guestToHost, .stopped),
             (.file, .hostToGuest, .readArtifact), (.file, .hostToGuest, .fileChunk),
             (.file, .guestToHost, .fileChunk),
             (.inference, .hostToGuest, .inferenceResponse),
             (.inference, .hostToGuest, .cancel),
             (.inference, .guestToHost, .inferenceRequest): true
        default: false
        }
    }
}

public enum RuntimeFrameError: Error, Equatable {
    case length, hash, json, duplicateJSONKey, messageType, direction, request, replay, backpressure
}

public struct RuntimeFrame: Equatable, Sendable {
    public static let zeroUUID = UUID(uuid: (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    public let header: ProtocolHeader
    public let payload: Data
    public let kind: RuntimeMessageKind

    public init(channel: RuntimeChannel, epoch: UInt64, request: UUID, payload: Data,
                direction: RuntimeDirection) throws {
        guard payload.count <= channel.payloadLimit else { throw RuntimeFrameError.length }
        let kind = try Self.kind(payload: payload, channel: channel, direction: direction)
        header = try ProtocolHeader(channel: channel, epoch: epoch, request: request,
                                    payloadLength: UInt32(payload.count),
                                    payloadSHA256: Array(SHA256.hash(data: payload)))
        self.payload = payload
        self.kind = kind
    }

    public init(headerBytes: [UInt8], payload: Data, expectedEpoch: UInt64,
                direction: RuntimeDirection) throws {
        let header = try ProtocolHeader(bytes: headerBytes, expectedEpoch: expectedEpoch)
        guard payload.count == header.payloadLength else { throw RuntimeFrameError.length }
        guard Array(SHA256.hash(data: payload)) == header.payloadSHA256 else {
            throw RuntimeFrameError.hash
        }
        guard header.request != Self.zeroUUID else { throw RuntimeFrameError.request }
        self.header = header
        self.payload = payload
        self.kind = try Self.kind(payload: payload, channel: header.channel, direction: direction)
    }

    public var data: Data { Data(header.bytes) + payload }

    private static func kind(payload: Data, channel: RuntimeChannel,
                             direction: RuntimeDirection) throws -> RuntimeMessageKind {
        let object = try StrictJSONObject.parse(payload)
        guard let name = object["type"] as? String,
              let kind = RuntimeMessageKind(rawValue: name) else {
            throw RuntimeFrameError.messageType
        }
        guard kind.allowed(channel: channel, direction: direction) else {
            throw RuntimeFrameError.direction
        }
        return kind
    }
}

public struct RuntimeRequestRegistry {
    private var local: Set<UUID> = []
    private var remote: Set<UUID> = []
    private let maximum: Int

    public init(maximum: Int = 4096) throws {
        guard maximum > 0, maximum <= 4096 else { throw RuntimeFrameError.backpressure }
        self.maximum = maximum
    }

    public mutating func registerLocal(_ request: UUID) throws {
        guard request != RuntimeFrame.zeroUUID, local.count < maximum else {
            throw RuntimeFrameError.backpressure
        }
        guard local.insert(request).inserted else { throw RuntimeFrameError.replay }
    }

    public mutating func acceptRemote(_ frame: RuntimeFrame, respondsToLocal: Bool) throws {
        if respondsToLocal {
            guard local.remove(frame.header.request) != nil else { throw RuntimeFrameError.request }
        } else {
            guard remote.count < maximum else { throw RuntimeFrameError.backpressure }
            guard remote.insert(frame.header.request).inserted else { throw RuntimeFrameError.replay }
        }
    }
}

public struct RuntimeChannelQueue {
    private let channel: RuntimeChannel
    private let limit: UInt64
    private var bytes: UInt64 = 0
    private var frames: [RuntimeFrame] = []

    public init(channel: RuntimeChannel) {
        self.channel = channel
        limit = channel.payloadLimit * (channel == .control ? 4 : 2)
    }

    public mutating func enqueue(_ frame: RuntimeFrame) throws {
        guard frame.header.channel == channel else { throw RuntimeFrameError.direction }
        let size = UInt64(ProtocolHeader.byteCount + frame.payload.count)
        guard size <= limit, bytes <= limit - size else { throw RuntimeFrameError.backpressure }
        bytes += size
        frames.append(frame)
    }

    public mutating func dequeue() -> RuntimeFrame? {
        guard !frames.isEmpty else { return nil }
        let frame = frames.removeFirst()
        bytes -= UInt64(ProtocolHeader.byteCount + frame.payload.count)
        return frame
    }
}

private enum StrictJSONObject {
    static func parse(_ data: Data) throws -> [String: Any] {
        guard String(data: data, encoding: .utf8) != nil else { throw RuntimeFrameError.json }
        var scanner = JSONDuplicateScanner(bytes: Array(data))
        do { try scanner.scan() } catch let error as RuntimeFrameError { throw error }
        catch { throw RuntimeFrameError.json }
        let value: Any
        do { value = try JSONSerialization.jsonObject(with: data, options: []) }
        catch { throw RuntimeFrameError.json }
        guard let object = value as? [String: Any] else { throw RuntimeFrameError.json }
        return object
    }
}

private struct JSONDuplicateScanner {
    let bytes: [UInt8]
    var index = 0

    mutating func scan() throws {
        space()
        try value(depth: 0)
        space()
        guard index == bytes.count else { throw RuntimeFrameError.json }
    }

    mutating func value(depth: Int) throws {
        guard depth < 64 else { throw RuntimeFrameError.json }
        space()
        guard let byte = peek else { throw RuntimeFrameError.json }
        switch byte {
        case 0x7b: try object(depth: depth)
        case 0x5b: try array(depth: depth)
        case 0x22: _ = try stringBytes()
        default:
            let start = index
            while let current = peek, ![0x2c, 0x5d, 0x7d, 0x20, 0x09, 0x0a, 0x0d].contains(current) {
                index += 1
            }
            guard index > start else { throw RuntimeFrameError.json }
        }
    }

    mutating func object(depth: Int) throws {
        try take(0x7b)
        space()
        var keys: Set<String> = []
        if peek == 0x7d { index += 1; return }
        while true {
            let raw = try stringBytes()
            let wrapped = Data([0x5b] + raw + [0x5d])
            guard let decoded = try? JSONSerialization.jsonObject(with: wrapped) as? [String],
                  let key = decoded.first else { throw RuntimeFrameError.json }
            guard keys.insert(key).inserted else { throw RuntimeFrameError.duplicateJSONKey }
            space(); try take(0x3a); try value(depth: depth + 1); space()
            if peek == 0x7d { index += 1; return }
            try take(0x2c); space()
        }
    }

    mutating func array(depth: Int) throws {
        try take(0x5b)
        space()
        if peek == 0x5d { index += 1; return }
        while true {
            try value(depth: depth + 1); space()
            if peek == 0x5d { index += 1; return }
            try take(0x2c); space()
        }
    }

    mutating func stringBytes() throws -> [UInt8] {
        let start = index
        try take(0x22)
        var escaped = false
        while let byte = peek {
            index += 1
            if escaped { escaped = false; continue }
            if byte == 0x5c { escaped = true; continue }
            if byte == 0x22 { return Array(bytes[start..<index]) }
            if byte < 0x20 { throw RuntimeFrameError.json }
        }
        throw RuntimeFrameError.json
    }

    mutating func take(_ expected: UInt8) throws {
        guard peek == expected else { throw RuntimeFrameError.json }
        index += 1
    }

    mutating func space() {
        while let byte = peek, [0x20, 0x09, 0x0a, 0x0d].contains(byte) { index += 1 }
    }

    var peek: UInt8? { index < bytes.count ? bytes[index] : nil }
}
