public enum InvalidWireID: Error { case invalid }
public protocol WireIDKind: Sendable { static var prefix: String { get } }
public struct WireID<Kind: WireIDKind>: Hashable, Sendable {
    public let rawValue: String
    public init(_ value: String) throws {
        let prefix = Kind.prefix + "_"
        guard value.hasPrefix(prefix) else { throw InvalidWireID.invalid }
        let suffix = value.dropFirst(prefix.count)
        guard !suffix.isEmpty, suffix.utf8.allSatisfy({
            (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0) || $0 == 45
        }) else { throw InvalidWireID.invalid }
        rawValue = value
    }
}
public enum SessionKind: WireIDKind { public static let prefix = "session" }
public enum TurnKind: WireIDKind { public static let prefix = "turn" }
public enum TaskKind: WireIDKind { public static let prefix = "task" }
public enum CallKind: WireIDKind { public static let prefix = "call" }
public enum FileVersionKind: WireIDKind { public static let prefix = "file" }
public typealias SessionID = WireID<SessionKind>
public typealias TurnID = WireID<TurnKind>
public typealias TaskID = WireID<TaskKind>
public typealias CallID = WireID<CallKind>
public typealias FileVersionID = WireID<FileVersionKind>
