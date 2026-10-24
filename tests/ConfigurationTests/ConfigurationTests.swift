import Foundation
import HavenboxCore
import CryptoKit

struct TestCredentials: CredentialStore {
    let present: Bool
    func save(reference: String, secret: String) throws {}
    func exists(reference: String) throws -> Bool { present }
    func read(reference: String) throws -> String { "synthetic" }
    func delete(reference: String) throws {}
}
struct TestTransport: EndpointTransport {
    let calls: Counter
    let response: EndpointCheckResponse
    func check(endpoint: URL, modelID: String, protocolName: String, prompt: String) async throws -> EndpointCheckResponse { calls.count += 1; if prompt != ConfigurationValidator.fixedTestPrompt { throw ConfigurationError.invalid("prompt") }; return response }
}
final class Counter: @unchecked Sendable { var count = 0 }

@main struct ConfigurationTests {
    static func require(_ value: Bool, _ message: String) throws { if !value { throw Failure(message) } }
    static func rejects(_ body: () throws -> Void, _ message: String) throws { do { try body() } catch { return }; throw Failure("accepted \(message)") }
    static func hashFixture() throws -> String {
        try ConfigurationValidator.digest(ModelConfiguration(id: "m", providerID: "p", protocolName: "chat-completions", contextLimit: 1000, outputLimit: 100, supportedParameters: ["temperature", "top_p", "seed"], capabilities: ["streaming", "tools", "json"]))
    }
    static func main() async throws {
        if CommandLine.arguments.contains("--hash") { print(try hashFixture()); return }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("hb16-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        let provider = ProviderConfiguration(id: "p", displayName: "Provider", deployment: .online, protocolName: "chat-completions", endpoint: "https://api.example.test/v1", credentialReference: "credential-ref")
        let model = ModelConfiguration(id: "m", providerID: "p", protocolName: "chat-completions", contextLimit: 1000, outputLimit: 100, supportedParameters: ["temperature"], capabilities: ["streaming"])
        let execution = ExecutionConfiguration(harnessRevision: "h1", providerID: "p", modelID: "m", deployment: .online, toolPolicyRevision: "tools-1", parameters: ["temperature": "0.2"])
        try ConfigurationValidator.validate(provider: provider, model: model, execution: execution)
        // An omitted optional parameter keeps its default/provider semantics; a
        // supplied temperature must be parseable, finite, and within 0...2.
        let omittedTemperature = ExecutionConfiguration(harnessRevision: "h1", providerID: "p", modelID: "m", deployment: .online, toolPolicyRevision: "tools-1")
        try ConfigurationValidator.validate(provider: provider, model: model, execution: omittedTemperature)
        for invalidTemperature in ["", "abc", "NaN", "inf", "-0.01", "2.01"] {
            let invalid = ExecutionConfiguration(harnessRevision: "h1", providerID: "p", modelID: "m", deployment: .online, toolPolicyRevision: "tools-1", parameters: ["temperature": invalidTemperature])
            try rejects({ try ConfigurationValidator.validate(provider: provider, model: model, execution: invalid) }, "temperature \(invalidTemperature.debugDescription)")
        }
        for validTemperature in ["0", "0.0", "2"] {
            let valid = ExecutionConfiguration(harnessRevision: "h1", providerID: "p", modelID: "m", deployment: .online, toolPolicyRevision: "tools-1", parameters: ["temperature": validTemperature])
            try ConfigurationValidator.validate(provider: provider, model: model, execution: valid)
        }
        try rejects({ try ConfigurationValidator.validate(provider: provider, model: model, execution: ExecutionConfiguration(harnessRevision: "h1", providerID: "p", modelID: "m", deployment: .online, toolPolicyRevision: "tools-1", parameters: ["reasoning": "high"])) }, "unsupported parameter")
        for endpoint in ["http://api.example.test", "https://localhost/api", "https://127.0.0.1/api", "https://10.0.0.1/api", "https://172.20.0.1/api", "https://169.254.169.254/latest", "https://[::1]/api", "https://metadata.google.internal/"] {
            try rejects({ try ConfigurationValidator.validate(provider: ProviderConfiguration(id: "p", displayName: "P", deployment: .online, protocolName: "chat-completions", endpoint: endpoint, credentialReference: "r"), model: model, execution: execution) }, "unsafe endpoint \(endpoint)")
        }
        let workspace = root.appendingPathComponent("workspace")
        let authWorkspace = try WorkspaceStore(directory: root.appendingPathComponent("auth-workspace"))
        do { _ = try await authWorkspace.saveConfiguration(objectID: "auth", kind: "provider", value: ProviderConfiguration(id: "p", displayName: "P", deployment: .online, protocolName: "chat-completions", endpoint: "https://user:SYNTHETIC_ONLY@api.example.test", credentialReference: "r")); throw Failure("URL credentials before persistence accepted") } catch is ConfigurationError { }
        var store: WorkspaceStore? = try WorkspaceStore(directory: workspace)
        let saved = try await store!.saveConfiguration(objectID: "p", kind: "provider", value: provider)
        let saved2 = try await store!.saveConfiguration(objectID: "p", kind: "provider", value: ProviderConfiguration(id: "p", displayName: "Changed", deployment: .online, protocolName: "chat-completions", endpoint: provider.endpoint, credentialReference: "credential-ref"))
        try require(saved.revision == 1 && saved2.revision == 2, "revisions increment")
        let loaded = try await store!.readConfiguration(objectID: "p")
        try require(loaded.revision == 2 && !loaded.body.contains("secret"), "latest configuration persisted")
        let digest = try ConfigurationValidator.digest(execution)
        let revisions = ("p1", "c1", "m1", "a1", "t1")
        let combination = try ConfigurationValidator.combinationDigest(provider: provider, model: model, execution: execution, providerRevision: revisions.0, credentialRevision: revisions.1, modelRevision: revisions.2, parameterRevision: revisions.3, toolsRevision: revisions.4)
        try require(Set((0..<100).map { _ in try! ConfigurationValidator.combinationDigest(provider: provider, model: model, execution: execution, providerRevision: revisions.0, credentialRevision: revisions.1, modelRevision: revisions.2, parameterRevision: revisions.3, toolsRevision: revisions.4) }).count == 1, "stable combination hash")
        // Multi-element sets and differently inserted dictionaries must hash identically.
        let richModel = ModelConfiguration(id: "m", providerID: "p", protocolName: "chat-completions", contextLimit: 1000, outputLimit: 100, supportedParameters: ["temperature", "top_p", "seed"], capabilities: ["streaming", "tools", "json"])
        let stable = try ConfigurationValidator.digest(richModel)
        let child = Process()
        let output = Pipe()
        child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        child.arguments = ["--hash"]
        child.standardOutput = output
        try child.run(); child.waitUntilExit()
        let childHash = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        try require(child.terminationStatus == 0 && childHash == stable, "canonical digest stable across process random seeds")
        for _ in 0..<100 {
            let rebuilt = ModelConfiguration(id: "m", providerID: "p", protocolName: "chat-completions", contextLimit: 1000, outputLimit: 100, supportedParameters: Set(["seed", "top_p", "temperature"].shuffled()), capabilities: Set(["json", "streaming", "tools"].shuffled()))
            try require(try ConfigurationValidator.digest(rebuilt) == stable, "100 rebuilt set hashes stable")
        }
        try require(try ConfigurationValidator.digest(["a": "1", "b": "2"]) == ConfigurationValidator.digest(["b": "2", "a": "1"]), "dictionary order stable")
        try require(try ConfigurationValidator.digest(["a", "b"]) != ConfigurationValidator.digest(["b", "a"]), "ordered arrays retain semantics")
        let persistedModel = try await store!.saveConfiguration(objectID: "model", kind: "model", value: richModel)
        try require(persistedModel.digest == stable, "storage and validator share canonical digest")
        let oldDigest = SHA256.hash(data: Data(try ConfigurationValidator.canonicalJSON(richModel).utf8)).map { String(format: "%02x", $0) }.joined()
        try await store!.saveCompatibilityCheck(CompatibilityCheck(digest: oldDigest, status: "passed", suiteVersion: "hb16-1", evidenceReference: "legacy"))
        try require(try await store!.compatibilityCheck(digest: stable, suiteVersion: "hb16-1") == nil, "legacy digest cannot qualify v2")
        for index in 0..<5 {
            var revs = ["p1", "c1", "m1", "a1", "t1"]
            revs[index] = "changed"
            let hash = try ConfigurationValidator.combinationDigest(provider: provider, model: model, execution: execution, providerRevision: revs[0], credentialRevision: revs[1], modelRevision: revs[2], parameterRevision: revs[3], toolsRevision: revs[4])
            try require(hash != combination, "every opaque revision invalidates evidence")
        }
        let alteredProviders = [
            ProviderConfiguration(id: "p", displayName: "Provider", deployment: .online, protocolName: "messages", endpoint: provider.endpoint),
            ProviderConfiguration(id: "p", displayName: "Provider", deployment: .online, protocolName: provider.protocolName, endpoint: "https://changed.example.test/v1")
        ]
        for altered in alteredProviders {
            let hash = try ConfigurationValidator.combinationDigest(provider: altered, model: model, execution: execution, providerRevision: "p1", credentialRevision: "c1", modelRevision: "m1", parameterRevision: "a1", toolsRevision: "t1")
            try require(hash != combination, "endpoint/protocol content invalidates without revision bump")
        }
        let alteredHash = try ConfigurationValidator.combinationDigest(provider: provider, model: richModel, execution: execution, providerRevision: "p1", credentialRevision: "c1", modelRevision: "m1", parameterRevision: "a1", toolsRevision: "t1")
        try require(alteredHash != combination, "capabilities invalidate without revision bump")
        let alteredExecution = ExecutionConfiguration(harnessRevision: "h1", providerID: "p", modelID: "m", deployment: .online, toolPolicyRevision: "tools-1", parameters: ["temperature": "0.8"])
        try require(try ConfigurationValidator.combinationDigest(provider: provider, model: model, execution: alteredExecution, providerRevision: "p1", credentialRevision: "c1", modelRevision: "m1", parameterRevision: "a1", toolsRevision: "t1") != combination, "parameter content invalidates without revision bump")
        let counter = Counter(); let checker = ConfigurationChecker()
        let authorization = ConfigurationAuthorization(prompt: ConfigurationValidator.fixedTestPrompt, target: provider.endpoint!, combinationDigest: combination, expiresAt: Int64(Date().timeIntervalSince1970) + 300, approved: true)
        let checked = try await checker.check(provider: provider, model: model, execution: execution, providerRevision: revisions.0, credentialRevision: revisions.1, modelRevision: revisions.2, parameterRevision: revisions.3, toolsRevision: revisions.4, authorization: authorization, credentials: TestCredentials(present: true), transport: TestTransport(calls: counter, response: EndpointCheckResponse(modelID: model.id, protocolName: provider.protocolName, testPrompt: ConfigurationValidator.fixedTestPrompt, contentDeltas: ["O", "K"], finished: true)))
        try require(checked.credentialExists && checked.connected && checked.capabilitiesKnown && counter.count == 1, "authorized fixed-prompt check")
        let changed = try ConfigurationValidator.combinationDigest(provider: provider, model: model, execution: execution, providerRevision: "changed", credentialRevision: revisions.1, modelRevision: revisions.2, parameterRevision: revisions.3, toolsRevision: revisions.4)
        try require(changed != combination, "revision invalidates authorization")
        do { _ = try await checker.check(provider: provider, model: model, execution: execution, providerRevision: "changed", credentialRevision: revisions.1, modelRevision: revisions.2, parameterRevision: revisions.3, toolsRevision: revisions.4, authorization: authorization, credentials: TestCredentials(present: true), transport: TestTransport(calls: counter, response: EndpointCheckResponse(modelID: model.id, protocolName: provider.protocolName, testPrompt: ConfigurationValidator.fixedTestPrompt, contentDeltas: ["O", "K"], finished: true))); throw Failure("stale authorization invoked transport") } catch is ConfigurationError { }
        try require(counter.count == 1, "stale authorization zero calls")
        try await store!.saveCompatibilityCheck(CompatibilityCheck(digest: digest, status: "passed", suiteVersion: "hb16-1", evidenceReference: "test-evidence"))
        try require(try await store!.compatibilityCheck(digest: digest, suiteVersion: "hb16-1")?.status == "passed", "compatibility state persisted")
        try require(try await store!.compatibilityCheck(digest: "changed", suiteVersion: "hb16-1") == nil, "changed config is stale")
        store = nil
        store = try WorkspaceStore(directory: workspace)
        try require(try await store!.readConfiguration(objectID: "p").revision == 2, "revision survives restart")
        try require(try await store!.compatibilityCheck(digest: digest, suiteVersion: "hb16-1")?.status == "passed", "compatibility survives restart")
        let reopenedModel = try await store!.readConfiguration(objectID: "model")
        try require(reopenedModel.digest == ConfigurationValidator.digest(JSONDecoder().decode(ModelConfiguration.self, from: Data(reopenedModel.body.utf8))), "reopened model reproduces digest")
        for status in ["passed", "failed", "passed"] {
            try await store!.saveCompatibilityCheck(CompatibilityCheck(digest: combination, status: status, suiteVersion: "hb16-1", evidenceReference: status))
            let state = try await checker.check(provider: provider, model: model, execution: execution, providerRevision: revisions.0, credentialRevision: revisions.1, modelRevision: revisions.2, parameterRevision: revisions.3, toolsRevision: revisions.4, authorization: authorization, credentials: TestCredentials(present: true), transport: TestTransport(calls: counter, response: EndpointCheckResponse(modelID: model.id, protocolName: provider.protocolName)), compatibility: store)
            try require(state.compatibilityStatus == status && !state.combinationPassed, "checker uses latest exact combination evidence but never grants execution")
        }
        // No transport is invoked by validation; real requests require a future explicit authorization gateway.
        print("PASS HB16 configuration, endpoint, revision and stale-check regressions")
    }
    struct Failure: Error, CustomStringConvertible { let message: String; init(_ message: String) { self.message = message }; var description: String { message } }
}
