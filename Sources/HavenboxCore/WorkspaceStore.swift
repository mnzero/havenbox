import Foundation
import CryptoKit

public struct WorkspaceMessage: Sendable, Equatable {
    public let id: String
    public let turnID: String
    public let sequence: Int64
    public let role: String
    public let body: String
    public let status: String
}

public struct WorkspaceEvent: Sendable, Equatable {
    public let id: String
    public let epoch: Int64
    public let sequence: Int64
    public let type: String
    public let payload: String
}

/// Trusted host-only metadata store. Opening never resumes requests, tools or old approvals.
/// The parent directory must be private and owned by the application account.
public actor WorkspaceStore {
    let database: WorkspaceDatabase
    public internal(set) var maintenanceError: String?
    public var isReadOnly: Bool { database.readOnly }

    public init(directory: URL) throws {
        guard directory.isFileURL else { throw WorkspaceStoreError("Workspace requires a local directory") }
        let directory = directory.standardizedFileURL
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let attributes = try fm.attributesOfItem(atPath: directory.path)
        guard attributes[.type] as? FileAttributeType == .typeDirectory,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              ((attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0) & 0o077 == 0,
              directory.resolvingSymlinksInPath().path == directory.path else {
            throw WorkspaceStoreError("Workspace directory must be private, owned and symlink-free")
        }
        guard let resolved = realpath(directory.path, nil) else { throw WorkspaceStoreError("Cannot resolve workspace directory") }
        defer { free(resolved) }
        let database = try WorkspaceDatabase(path: String(cString: resolved) + "/workspace.db")
        let version = try database.query("PRAGMA user_version")[0][0]!
        if version == "0" {
            try database.transaction {
                try database.execute(WorkspaceSchema.sql + WorkspaceSchema.referenceGuards)
                try database.execute("PRAGMA user_version=1")
            }
        } else if version != String(WorkspaceSchema.version) {
            try database.becomeReadOnly()
            maintenanceError = "Incompatible schema \(version); history only"
        } else if !(try database.query("SELECT id FROM maintenance_jobs WHERE kind='migration' AND state!='complete' LIMIT 1")).isEmpty {
            try database.becomeReadOnly()
            maintenanceError = "Unfinished migration; restore verified backup before execution"
        }
        self.database = database
    }

    public func createSession(id: String, title: String, recipeRevision: String?, toolPolicyRevision: String?) throws {
        try database.transaction {
            try database.query("INSERT INTO sessions VALUES(?,?,?,?,?,'ready',?)", [id, title, recipeRevision == nil ? "toolsOnly" : "agent", recipeRevision, toolPolicyRevision, String(Int64(Date().timeIntervalSince1970))])
            try appendEvent(type: "session.created", payload: "{}", sessionID: id)
        }
    }

    /// Stable command IDs survive reconnects; reusing an ID with different input is an error.
    public func sendMessage(sessionID: String, commandID: String, body: String) throws -> String {
        // Length-prefixed input is an unambiguous local identity, not an authorization digest.
        let identity = Self.commandDigest(["send", sessionID, body])
        return try database.transaction {
            if let row = try database.query("SELECT request_digest,result FROM commands WHERE id=?", [commandID]).first {
                guard row[0] == identity else { throw WorkspaceStoreError("Command ID reused with different input") }
                return row[1]!
            }
            guard try database.query("SELECT state FROM sessions WHERE id=?", [sessionID]).first?[0] == "ready" else { throw WorkspaceStoreError("Session is not writable") }
            let turnID = UUID().uuidString
            let messageID = UUID().uuidString
            try database.query("INSERT INTO turns(id,session_id,seq,idempotency_key,state) SELECT ?,?,COALESCE(MAX(seq),0)+1,?,'pending' FROM turns WHERE session_id=?", [turnID, sessionID, commandID, sessionID])
            try database.query("INSERT INTO active_slot VALUES(1,?,(SELECT epoch FROM workspaces WHERE id=1),'active')", [turnID])
            try database.query("INSERT INTO messages SELECT ?,?,?,COALESCE(MAX(seq),0)+1,'user',?,'complete' FROM messages WHERE session_id=?", [messageID, turnID, sessionID, body, sessionID])
            try database.query("INSERT INTO commands VALUES(?,?,?)", [commandID, identity, turnID])
            try appendEvent(type: "message.created", payload: "{}", sessionID: sessionID, turnID: turnID)
            return turnID
        }
    }

    public func readHistory(sessionID: String, after sequence: Int64 = 0, limit: Int = 100) throws -> [WorkspaceMessage] {
        guard (1...1000).contains(limit) else { throw WorkspaceStoreError("Invalid page size") }
        return try database.query("SELECT id,turn_id,seq,role,body,status FROM messages WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", [sessionID, String(sequence), String(limit)]).map {
            WorkspaceMessage(id: $0[0]!, turnID: $0[1]!, sequence: Int64($0[2]!)!, role: $0[3]!, body: $0[4]!, status: $0[5]!)
        }
    }

    public func readEvents(epoch: Int64, after sequence: Int64 = 0, limit: Int = 100) throws -> [WorkspaceEvent] {
        guard (1...1000).contains(limit) else { throw WorkspaceStoreError("Invalid page size") }
        return try database.query("SELECT id,workspace_epoch,seq,type,payload FROM events WHERE workspace_epoch=? AND seq>? ORDER BY seq LIMIT ?", [String(epoch), String(sequence), String(limit)]).map {
            WorkspaceEvent(id: $0[0]!, epoch: Int64($0[1]!)!, sequence: Int64($0[2]!)!, type: $0[3]!, payload: $0[4]!)
        }
    }

    // Only invoke inside a short transaction containing the related state change.
    func appendEvent(type: String, payload: String, sessionID: String? = nil, turnID: String? = nil) throws {
        try database.query("INSERT INTO events SELECT ?,epoch,next_seq,?,?,?,? FROM event_counters WHERE epoch=(SELECT epoch FROM workspaces WHERE id=1)", [UUID().uuidString, sessionID, turnID, type, payload])
        try database.execute("UPDATE event_counters SET next_seq=next_seq+1 WHERE epoch=(SELECT epoch FROM workspaces WHERE id=1)")
    }

    static func commandDigest(_ fields: [String]) -> String {
        let encoded = fields.map { "\($0.utf8.count):\($0)" }.joined()
        return SHA256.hash(data: Data(encoded.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    public func backup(to destination: URL) throws {
        guard destination.isFileURL else { throw WorkspaceStoreError("Backup requires local file URL") }
        try database.backup(to: destination.path)
    }
    public func checkpoint() throws { try database.checkpoint() }
}
