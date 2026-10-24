import Foundation
import HavenboxCore

// Dependency-free executable unit suite: also runs on CLT-only machines without XCTest.
@main
struct ProtocolTests {
    static func main() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let text = try String(contentsOf: root.appendingPathComponent("contracts/header-vectors.tsv"), encoding: .utf8)
        var count = 0
        for line in text.split(separator: "\n") where !line.hasPrefix("#") {
            let fields = line.split(separator: "\t", omittingEmptySubsequences: false)
            let hex = Array(fields[1])
            let bytes = stride(from: 0, to: hex.count, by: 2).map { index in
                UInt8(String(hex[index...index + 1]), radix: 16)!
            }
            let actual: String
            do {
                _ = try ProtocolHeader(bytes: bytes, expectedEpoch: 7)
                actual = "ok"
            } catch {
                actual = String(describing: error).lowercased()
            }
            try check(actual == fields[2], String(fields[0]))
            count += 1
        }
        try check(try SessionID("session_A-1").rawValue == "session_A-1", "valid ID")
        for value in ["turn_A", "session_", "session_é", "session_a/b"] {
            do {
                _ = try SessionID(value)
                throw TestFailure(message: "accepted invalid ID: \(value)")
            } catch is InvalidWireID { }
            count += 1
        }
        _ = try TurnID("turn_1")
        _ = try TaskID("task_1")
        _ = try CallID("call_1")
        _ = try FileVersionID("file_1")
        let request = UUID(uuidString: "01010101-0101-0101-0101-010101010101")!
        let hello = try RuntimeFrame(channel: .control, epoch: 7, request: request,
                                     payload: Data(#"{"type":"Hello","instance":"run"}"#.utf8),
                                     direction: .hostToGuest)
        let decoded = try RuntimeFrame(headerBytes: Array(hello.data.prefix(ProtocolHeader.byteCount)),
                                       payload: hello.data.dropFirst(ProtocolHeader.byteCount),
                                       expectedEpoch: 7, direction: .hostToGuest)
        try check(decoded == hello, "full frame round trip")
        var bad = hello.payload
        bad[bad.startIndex] ^= 1
        do {
            _ = try RuntimeFrame(headerBytes: hello.header.bytes, payload: bad,
                                 expectedEpoch: 7, direction: .hostToGuest)
            throw TestFailure(message: "accepted bad payload hash")
        } catch RuntimeFrameError.hash { count += 1 }
        do {
            _ = try RuntimeFrame(channel: .control, epoch: 7, request: UUID(),
                                 payload: Data(#"{"type":"Hello","type":"Stop"}"#.utf8),
                                 direction: .hostToGuest)
            throw TestFailure(message: "accepted duplicate JSON key")
        } catch RuntimeFrameError.duplicateJSONKey { count += 1 }
        do {
            _ = try RuntimeFrame(channel: .control, epoch: 7, request: UUID(),
                                 payload: Data(#"{"type":"HelloAck"}"#.utf8),
                                 direction: .hostToGuest)
            throw TestFailure(message: "accepted wrong message direction")
        } catch RuntimeFrameError.direction { count += 1 }
        let nested = Data((#"{"type":"Hello","value":"# + String(repeating: "[", count: 65)
            + String(repeating: "]", count: 65) + "}").utf8)
        do {
            _ = try RuntimeFrame(channel: .control, epoch: 7, request: UUID(),
                                 payload: nested, direction: .hostToGuest)
            throw TestFailure(message: "accepted excessively nested JSON")
        } catch RuntimeFrameError.json { count += 1 }
        var registry = try RuntimeRequestRegistry(maximum: 2)
        try registry.registerLocal(request)
        try registry.acceptRemote(
            RuntimeFrame(channel: .control, epoch: 7, request: request,
                         payload: Data(#"{"type":"HelloAck"}"#.utf8),
                         direction: .guestToHost),
            respondsToLocal: true
        )
        do {
            try registry.acceptRemote(
                RuntimeFrame(channel: .control, epoch: 7, request: request,
                             payload: Data(#"{"type":"HelloAck"}"#.utf8),
                             direction: .guestToHost),
                respondsToLocal: true
            )
            throw TestFailure(message: "accepted unknown/replayed response")
        } catch RuntimeFrameError.request { count += 1 }
        do {
            try EngineeringBuild.requireExecution()
        } catch EngineeringBuild.Failure.runtimeNotImplemented { count += 1 }
        print("Swift core: \(count) regression cases passed")
    }
    struct TestFailure: Error { let message: String }
    static func check(_ value: Bool, _ message: String) throws {
        if !value { throw TestFailure(message: message) }
    }
}
