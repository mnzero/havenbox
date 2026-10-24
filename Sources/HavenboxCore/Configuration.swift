import Foundation
import CryptoKit
#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif
#if canImport(Security)
import Security
#endif

public enum ConfigurationError: Error, Equatable, CustomStringConvertible, Sendable {
    case invalid(String)
    case unsupportedParameter(String)
    case credentialUnavailable
    case credentialOperation(String, Int32)
    case staleCheck
    case endpointRejected
    case notAuthorized
    case expiredAuthorization
    case transportFailure(String)
    public var description: String {
        switch self { case .invalid(let s): return "Invalid configuration: \(s)"; case .unsupportedParameter(let s): return "Unsupported parameter: \(s)"; case .credentialUnavailable: return "Credential unavailable"; case .credentialOperation(let s, let code): return "Credential operation \(s) failed (status \(code))"; case .staleCheck: return "Compatibility check is stale"; case .endpointRejected: return "Endpoint rejected"; case .notAuthorized: return "Authorization required"; case .expiredAuthorization: return "Authorization expired"; case .transportFailure(let s): return "Transport failure: \(s)" }
    }
}

public enum ModelDeployment: String, Codable, Sendable { case local, online }
public struct ProviderConfiguration: Codable, Equatable, Sendable {
    public let id: String; public let displayName: String; public let deployment: ModelDeployment
    public let protocolName: String; public let endpoint: String?; public let credentialReference: String?
    public init(id: String, displayName: String, deployment: ModelDeployment, protocolName: String, endpoint: String? = nil, credentialReference: String? = nil) {
        self.id=id; self.displayName=displayName; self.deployment=deployment; self.protocolName=protocolName; self.endpoint=endpoint; self.credentialReference=credentialReference
    }
}
public struct ModelConfiguration: Codable, Equatable, Sendable {
    public let id: String; public let providerID: String; public let contextLimit: Int; public let outputLimit: Int
    public let supportedParameters: Set<String>; public let capabilities: Set<String>; public let protocolName: String
    public init(id: String, providerID: String, protocolName: String, contextLimit: Int, outputLimit: Int, supportedParameters: Set<String> = [], capabilities: Set<String> = []) { self.id=id; self.providerID=providerID; self.protocolName=protocolName; self.contextLimit=contextLimit; self.outputLimit=outputLimit; self.supportedParameters=supportedParameters; self.capabilities=capabilities }
    enum CodingKeys: String, CodingKey { case id, providerID, protocolName, contextLimit, outputLimit, supportedParameters, capabilities }
    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id)
        try values.encode(providerID, forKey: .providerID)
        try values.encode(protocolName, forKey: .protocolName)
        try values.encode(contextLimit, forKey: .contextLimit)
        try values.encode(outputLimit, forKey: .outputLimit)
        try values.encode(supportedParameters.sorted(), forKey: .supportedParameters)
        try values.encode(capabilities.sorted(), forKey: .capabilities)
    }
}
public struct LocalResourceConfiguration: Codable, Equatable, Sendable { public let id: String; public let revision: String; public let sha256: String; public init(id: String, revision: String, sha256: String) { self.id=id; self.revision=revision; self.sha256=sha256 } }
public struct ExecutionConfiguration: Codable, Equatable, Sendable {
    public let harnessRevision: String; public let providerID: String; public let modelID: String; public let deployment: ModelDeployment; public let toolPolicyRevision: String; public let parameters: [String: String]
    public init(harnessRevision: String, providerID: String, modelID: String, deployment: ModelDeployment, toolPolicyRevision: String, parameters: [String: String] = [:]) { self.harnessRevision=harnessRevision; self.providerID=providerID; self.modelID=modelID; self.deployment=deployment; self.toolPolicyRevision=toolPolicyRevision; self.parameters=parameters }
}

public struct ConfigurationAuthorization: Codable, Equatable, Sendable {
    public let prompt: String
    public let target: String
    public let combinationDigest: String
    public let expiresAt: Int64
    public let approved: Bool
    public init(prompt: String, target: String, combinationDigest: String, expiresAt: Int64, approved: Bool) { self.prompt = prompt; self.target = target; self.combinationDigest = combinationDigest; self.expiresAt = expiresAt; self.approved = approved }
    public func isValid(now: Int64 = Int64(Date().timeIntervalSince1970), digest: String) -> Bool { approved && prompt == ConfigurationValidator.fixedTestPrompt && !target.isEmpty && combinationDigest == digest && expiresAt > now }
}
public struct ConfigurationCheckState: Codable, Equatable, Sendable {
    public let credentialExists: Bool; public let connected: Bool; public let capabilitiesKnown: Bool; public let combinationPassed: Bool
    /// Configuration declarations are never evidence; this is `verified` only after a matching response.
    public let capabilityStatus: String
    public let compatibilityStatus: String
    public init(credentialExists: Bool, connected: Bool, capabilitiesKnown: Bool, combinationPassed: Bool, capabilityStatus: String, compatibilityStatus: String) { self.credentialExists=credentialExists; self.connected=connected; self.capabilitiesKnown=capabilitiesKnown; self.combinationPassed=combinationPassed; self.capabilityStatus=capabilityStatus; self.compatibilityStatus=compatibilityStatus }
}
public struct EndpointCheckResponse: Sendable, Equatable {
    public let modelID: String; public let protocolName: String
    /// Normalized structured observation from the adapter, not a provider capability declaration.
    /// HB16 only recognizes the fixed streaming probe: two ordered content deltas and a terminal marker.
    public let testPrompt: String; public let contentDeltas: [String]; public let finished: Bool
    public init(modelID: String, protocolName: String, testPrompt: String = "", contentDeltas: [String] = [], finished: Bool = false) { self.modelID=modelID; self.protocolName=protocolName; self.testPrompt=testPrompt; self.contentDeltas=contentDeltas; self.finished=finished }
}
public struct CompatibilityCheck: Codable, Equatable, Sendable {
    public let digest: String; public let status: String; public let suiteVersion: String; public let evidenceReference: String
    public init(digest: String, status: String, suiteVersion: String, evidenceReference: String) { self.digest=digest; self.status=status; self.suiteVersion=suiteVersion; self.evidenceReference=evidenceReference }
}
public protocol EndpointTransport: Sendable { func check(endpoint: URL, modelID: String, protocolName: String, prompt: String) async throws -> EndpointCheckResponse }
public protocol CredentialStore: Sendable { func save(reference: String, secret: String) throws; func exists(reference: String) throws -> Bool; func read(reference: String) throws -> String; func delete(reference: String) throws }
public struct ConfigurationChecker {
    public init() {}
    /// No request is made until the caller supplies an approved, unexpired authorization for this exact digest.
    public func check(provider: ProviderConfiguration, model: ModelConfiguration, execution: ExecutionConfiguration, providerRevision: String, credentialRevision: String, modelRevision: String, parameterRevision: String, toolsRevision: String, authorization: ConfigurationAuthorization, credentials: CredentialStore, transport: EndpointTransport, compatibility: WorkspaceStore? = nil, suiteVersion: String = "hb16-1") async throws -> ConfigurationCheckState {
        try ConfigurationValidator.validate(provider: provider, model: model, execution: execution)
        let digest = try ConfigurationValidator.combinationDigest(provider: provider, model: model, execution: execution, providerRevision: providerRevision, credentialRevision: credentialRevision, modelRevision: modelRevision, parameterRevision: parameterRevision, toolsRevision: toolsRevision)
        guard authorization.isValid(digest: digest), authorization.target == provider.endpoint else { throw authorization.expiresAt <= Int64(Date().timeIntervalSince1970) ? ConfigurationError.expiredAuthorization : ConfigurationError.notAuthorized }
        let exists = try credentials.exists(reference: provider.credentialReference ?? "")
        guard exists else { throw ConfigurationError.credentialUnavailable }
        guard let raw = provider.endpoint, let endpoint = URL(string: raw) else { throw ConfigurationError.endpointRejected }
        let response: EndpointCheckResponse
        do { response = try await transport.check(endpoint: endpoint, modelID: model.id, protocolName: provider.protocolName, prompt: Self.fixedPrompt) } catch { throw ConfigurationError.transportFailure(String(describing: error)) }
        // A successful connection is observation only. Evidence must identify the exact model/protocol and every declared capability.
        let capabilityMatch = response.modelID == model.id && response.protocolName == provider.protocolName && response.testPrompt == Self.fixedPrompt && response.contentDeltas == ["O", "K"] && response.finished && model.capabilities == ["streaming"]
        let latest = try await compatibility?.compatibilityCheck(digest: digest, suiteVersion: suiteVersion)
        let status = latest?.status ?? "unknown"
        // This candidate has no frozen runtime/qualification evidence. A stored pass is not a
        // ValidationRunGrant and must never enable normal execution (HB17/HB31 own that gate).
        return ConfigurationCheckState(credentialExists: true, connected: true, capabilitiesKnown: capabilityMatch, combinationPassed: false, capabilityStatus: capabilityMatch ? "verified" : (model.capabilities.isEmpty ? "unverified" : "declared"), compatibilityStatus: status)
    }
    public static let fixedPrompt = ConfigurationValidator.fixedTestPrompt
}

#if canImport(Security)
public struct KeychainCredentialStore: CredentialStore {
    private let service: String
    /// Tests must pass a unique service; the default is the production namespace.
    public init(service: String = "com.havenbox.credentials") { self.service = service }
    public func save(reference: String, secret: String) throws { let data=Data(secret.utf8); let query:[CFString:Any] = [kSecClass:kSecClassGenericPassword,kSecAttrService:service,kSecAttrAccount:reference]; let status=SecItemUpdate(query as CFDictionary,[kSecValueData:data] as CFDictionary); if status == errSecItemNotFound { let add=SecItemAdd((query.merging([kSecValueData:data]) { _, new in new }) as CFDictionary,nil); guard add == errSecSuccess else { throw ConfigurationError.credentialOperation("add", add) } } else if status != errSecSuccess { throw ConfigurationError.credentialOperation("update", status) } }
    public func exists(reference: String) throws -> Bool { let q:[CFString:Any] = [kSecClass:kSecClassGenericPassword,kSecAttrService:service,kSecAttrAccount:reference,kSecReturnData:false,kSecMatchLimit:kSecMatchLimitOne]; let status=SecItemCopyMatching(q as CFDictionary,nil); if status == errSecSuccess { return true }; if status == errSecItemNotFound { return false }; throw ConfigurationError.credentialOperation("exists", status) }
    public func read(reference: String) throws -> String { let q:[CFString:Any] = [kSecClass:kSecClassGenericPassword,kSecAttrService:service,kSecAttrAccount:reference,kSecReturnData:true,kSecMatchLimit:kSecMatchLimitOne]; var result:CFTypeRef?; let status=SecItemCopyMatching(q as CFDictionary,&result); guard status == errSecSuccess, let data=result as? Data, let value=String(data:data,encoding:.utf8) else { throw ConfigurationError.credentialOperation("read", status) }; return value }
    public func delete(reference: String) throws { let q:[CFString:Any] = [kSecClass:kSecClassGenericPassword,kSecAttrService:service,kSecAttrAccount:reference]; let status=SecItemDelete(q as CFDictionary); guard status == errSecSuccess || status == errSecItemNotFound else { throw ConfigurationError.credentialOperation("delete", status) } }
}
#else
public struct KeychainCredentialStore: CredentialStore {
    public init(service: String = "com.havenbox.credentials") {}
    private func unavailable() throws -> Never { throw ConfigurationError.credentialUnavailable }; public func save(reference: String, secret: String) throws { try unavailable() }; public func exists(reference: String) throws -> Bool { try unavailable() }; public func read(reference: String) throws -> String { try unavailable() }; public func delete(reference: String) throws { try unavailable() } }
#endif

public enum ConfigurationValidator {
    public static let fixedTestPrompt = "Havenbox streaming capability check v1: stream exactly O then K, then finish; no files or tools"
    public static let supportedProtocols: Set<String> = ["chat-completions", "messages"]
    public static func validate(provider: ProviderConfiguration, model: ModelConfiguration, execution: ExecutionConfiguration) throws {
        guard provider.id == model.providerID, provider.id == execution.providerID, model.id == execution.modelID else { throw ConfigurationError.invalid("provider/model reference") }
        guard supportedProtocols.contains(provider.protocolName) else { throw ConfigurationError.invalid("unsupported protocol") }
        guard model.protocolName == provider.protocolName else { throw ConfigurationError.invalid("protocol/model mismatch") }
        guard execution.deployment == provider.deployment else { throw ConfigurationError.invalid("deployment mismatch") }
        guard model.contextLimit > 0, model.outputLimit > 0, model.outputLimit <= model.contextLimit else { throw ConfigurationError.invalid("model limits") }
        for key in execution.parameters.keys {
            guard model.supportedParameters.contains(key) else { throw ConfigurationError.unsupportedParameter(key) }
            if key == "temperature" {
                // A supplied temperature is an explicit user input: it must parse as
                // a finite number in the provider-independent inclusive range.
                guard let raw = execution.parameters[key], let value = Double(raw), value.isFinite, (0...2).contains(value) else {
                    throw ConfigurationError.invalid("temperature")
                }
            }
        }
        if provider.deployment == .online {
            guard let raw=provider.endpoint, let url=URL(string:raw), url.scheme?.lowercased() == "https", url.user == nil, url.password == nil, url.query == nil, url.fragment == nil, let host=url.host, !host.isEmpty, !isPrivateOrLoopback(host) else { throw ConfigurationError.endpointRejected }
            guard provider.credentialReference != nil, !(provider.credentialReference?.isEmpty ?? true) else { throw ConfigurationError.credentialUnavailable }
        }
    }
    private static func isPrivateOrLoopback(_ host: String) -> Bool {
        // URL parsers accept ambiguous IPv4 forms (127.1, decimal integers); never route them.
        let pieces = host.split(separator: ".")
        if pieces.count != 4 && host.allSatisfy({ $0.isNumber || $0 == "." }) { return true }
        if host.allSatisfy({ $0.isNumber }) { return true }
        let lower=host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if lower == "localhost" || lower == "localhost." || lower == "metadata.google.internal" || lower == "metadata" { return true }
        var v4 = in_addr(); if lower.withCString({ inet_pton(AF_INET, $0, &v4) }) == 1 {
            let n = UInt32(bigEndian: v4.s_addr)
            let a = n >> 24, b = (n >> 16) & 255
            return a == 10 || a == 127 || (a == 169 && b == 254) || (a == 172 && (16...31).contains(b)) || (a == 192 && b == 168) || n == 0
        }
        var v6 = in6_addr(); if lower.withCString({ inet_pton(AF_INET6, $0, &v6) }) == 1 {
            let b = withUnsafeBytes(of: v6) { Array($0) }
            if b[0..<10].allSatisfy({ $0 == 0 }) && b[10] == 0xff && b[11] == 0xff { return true }
            return b.allSatisfy({ $0 == 0 }) || (b.dropLast().allSatisfy({ $0 == 0 }) && b[15] == 1) || b[0] == 0xfc || b[0] == 0xfd || (b[0] == 0xfe && (b[1] & 0xc0) == 0x80)
        }
        return false
    }
    public static func combinationDigest(provider: ProviderConfiguration, model: ModelConfiguration, execution: ExecutionConfiguration, providerRevision: String, credentialRevision: String, modelRevision: String, parameterRevision: String, toolsRevision: String) throws -> String {
        // Deliberately exclude credentialReference: the only credential identity in a
        // combination is its opaque revision, never an account name or secret.
        struct PublicProvider: Encodable { let id: String; let displayName: String; let deployment: ModelDeployment; let protocolName: String; let endpoint: String? }
        struct Combination: Encodable { let provider: PublicProvider; let model: ModelConfiguration; let execution: ExecutionConfiguration; let providerRevision: String; let credentialRevision: String; let modelRevision: String; let parameterRevision: String; let toolsRevision: String }
        let publicProvider = PublicProvider(id: provider.id, displayName: provider.displayName, deployment: provider.deployment, protocolName: provider.protocolName, endpoint: provider.endpoint)
        return try digest(Combination(provider: publicProvider, model: model, execution: execution, providerRevision: providerRevision, credentialRevision: credentialRevision, modelRevision: modelRevision, parameterRevision: parameterRevision, toolsRevision: toolsRevision))
    }
    /// Shared canonical representation used both for persisted revisions and compatibility evidence.
    /// Model sets encode as sorted strings; object keys sort recursively. Ordered arrays retain order.
    /// v2 domain separation invalidates legacy noncanonical evidence without rewriting history.
    public static func canonicalJSON<T: Encodable>(_ value: T) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return String(decoding: try encoder.encode(value), as: UTF8.self)
    }
    public static func digest<T: Encodable>(_ value: T) throws -> String {
        let data = Data(("havenbox.configuration.v2\n" + (try canonicalJSON(value))).utf8)
        return SHA256.hash(data:data).map { String(format:"%02x",$0) }.joined()
    }
}
