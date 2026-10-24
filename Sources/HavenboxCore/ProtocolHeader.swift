/// Header-only preflight. Payload decoding, authentication and transport belong to HB-11.
public struct ProtocolHeader: Equatable, Sendable {
    public static let byteCount = 68
    public static let version: UInt16 = 1
    public let channel: UInt8
    public let epoch: UInt64
    public let payloadLength: UInt32

    public enum Failure: Error, Equatable {
        case size, magic, version, channel, flags, epoch, payloadLimit
    }

    public init(bytes: [UInt8], expectedEpoch: UInt64) throws {
        guard bytes.count == Self.byteCount else { throw Failure.size }
        guard Array(bytes[0..<4]) == Array("HBX1".utf8) else { throw Failure.magic }
        guard Self.number(bytes[4..<6]) == UInt64(Self.version) else { throw Failure.version }
        let channel = bytes[6]
        let limit: UInt64
        switch channel {
        case 1: limit = 65_536
        case 2: limit = 262_144
        case 3: limit = 4_194_304
        default: throw Failure.channel
        }
        guard bytes[7] == 0 else { throw Failure.flags }
        let epoch = Self.number(bytes[8..<16])
        guard epoch == expectedEpoch else { throw Failure.epoch }
        let length = Self.number(bytes[32..<36])
        guard length <= limit else { throw Failure.payloadLimit }
        self.channel = channel
        self.epoch = epoch
        self.payloadLength = UInt32(length)
    }

    private static func number(_ bytes: ArraySlice<UInt8>) -> UInt64 {
        bytes.reduce(0) { ($0 << 8) | UInt64($1) }
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
