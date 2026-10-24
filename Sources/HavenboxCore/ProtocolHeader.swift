import Foundation

/// Fixed hb.channel.v1 header. RuntimeFrame owns payload and direction validation.
public struct ProtocolHeader: Equatable, Sendable {
    public static let byteCount = 68
    public static let version: UInt16 = 1
    public let channel: RuntimeChannel
    public let epoch: UInt64
    public let request: UUID
    public let payloadLength: UInt32
    public let payloadSHA256: [UInt8]

    public enum Failure: Error, Equatable {
        case size, magic, version, channel, flags, epoch, payloadLimit
    }

    public init(bytes: [UInt8], expectedEpoch: UInt64) throws {
        guard bytes.count == Self.byteCount else { throw Failure.size }
        guard Array(bytes[0..<4]) == Array("HBX1".utf8) else { throw Failure.magic }
        guard Self.number(bytes[4..<6]) == UInt64(Self.version) else { throw Failure.version }
        guard let channel = RuntimeChannel(rawValue: bytes[6]) else { throw Failure.channel }
        guard bytes[7] == 0 else { throw Failure.flags }
        let epoch = Self.number(bytes[8..<16])
        guard epoch == expectedEpoch else { throw Failure.epoch }
        let length = Self.number(bytes[32..<36])
        guard length <= channel.payloadLimit else { throw Failure.payloadLimit }
        self.channel = channel
        self.epoch = epoch
        let requestBytes = Array(bytes[16..<32])
        self.request = UUID(uuid: (
            requestBytes[0], requestBytes[1], requestBytes[2], requestBytes[3],
            requestBytes[4], requestBytes[5], requestBytes[6], requestBytes[7],
            requestBytes[8], requestBytes[9], requestBytes[10], requestBytes[11],
            requestBytes[12], requestBytes[13], requestBytes[14], requestBytes[15]
        ))
        self.payloadLength = UInt32(length)
        self.payloadSHA256 = Array(bytes[36..<68])
    }

    public init(channel: RuntimeChannel, epoch: UInt64, request: UUID,
                payloadLength: UInt32, payloadSHA256: [UInt8]) throws {
        guard epoch > 0, UInt64(payloadLength) <= channel.payloadLimit,
              request != RuntimeFrame.zeroUUID, payloadSHA256.count == 32 else {
            throw Failure.payloadLimit
        }
        self.channel = channel
        self.epoch = epoch
        self.request = request
        self.payloadLength = payloadLength
        self.payloadSHA256 = payloadSHA256
    }

    public var bytes: [UInt8] {
        var result = Array("HBX1".utf8) + [0, 1, channel.rawValue, 0]
        result += Self.bigEndian(epoch, count: 8)
        var value = request.uuid
        result += withUnsafeBytes(of: &value) { Array($0) }
        result += Self.bigEndian(UInt64(payloadLength), count: 4)
        result += payloadSHA256
        return result
    }

    private static func number(_ bytes: ArraySlice<UInt8>) -> UInt64 {
        bytes.reduce(0) { ($0 << 8) | UInt64($1) }
    }

    private static func bigEndian(_ value: UInt64, count: Int) -> [UInt8] {
        (0..<count).reversed().map { UInt8(truncatingIfNeeded: value >> UInt64($0 * 8)) }
    }
}

/// The engineering shell has no execution or grant issuance capability in any configuration.
public enum EngineeringBuild {
    public static let status = "HB-09 candidate: product execution unavailable"
    public enum Failure: Error { case runtimeNotImplemented }
    public static func requireExecution() throws -> Never {
        throw Failure.runtimeNotImplemented
    }
}
