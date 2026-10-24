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
