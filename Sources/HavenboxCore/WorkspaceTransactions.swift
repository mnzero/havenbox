import Foundation

extension WorkspaceStore {
    public func recordDisclosure(turnID: String, digest: String, expiresAt: Int64) throws -> String {
        let id = UUID().uuidString
        try database.transaction {
            try database.query("INSERT INTO approvals VALUES(?,?,NULL,'disclosure',?,(SELECT epoch FROM workspaces WHERE id=1),?,NULL)", [id, turnID, digest, String(expiresAt)])
            try appendEvent(type: "disclosure.recorded", payload: "{}", turnID: turnID)
        }
        return id
    }

    public func savePlan(id: String, revision: Int, turnID: String, canonicalJSON: String, digest: String) throws {
        try database.transaction {
            try database.query("INSERT INTO plans VALUES(?,?,?,?,?)", [id, String(revision), turnID, canonicalJSON, digest])
            try appendEvent(type: "plan.recorded", payload: "{}", turnID: turnID)
        }
    }

    /// Records intent only. No permission token or tool dispatch is issued by this module.
    public func approveExecution(commandID: String, turnID: String, planID: String, revision: Int, digest: String, snapshotJSON: String, expiresAt: Int64) throws -> String {
        let fields = ["approve", turnID, planID, String(revision), digest, snapshotJSON, String(expiresAt)]
        let identity = Self.commandDigest(fields)
        return try database.transaction {
            if let row = try database.query("SELECT request_digest,result FROM commands WHERE id=?", [commandID]).first {
                guard row[0] == identity else { throw WorkspaceStoreError("Command ID reused with different input") }
                return row[1]!
            }
            guard try database.query("SELECT turn_id FROM active_slot WHERE singleton_id=1").first?[0] == turnID else { throw WorkspaceStoreError("Turn does not own active slot") }
            let taskID = UUID().uuidString
            try database.query("INSERT INTO tasks VALUES(?,?,?,?,?,?,'approved')", [taskID, turnID, planID, String(revision), digest, snapshotJSON])
            try database.query("INSERT INTO approvals VALUES(?,?,?,'execution',?,(SELECT epoch FROM workspaces WHERE id=1),?,NULL)", [UUID().uuidString, turnID, taskID, digest, String(expiresAt)])
            try database.query("INSERT INTO commands VALUES(?,?,?)", [commandID, identity, taskID])
            try appendEvent(type: "execution.approved", payload: "{}", turnID: turnID)
            return taskID
        }
    }

    /// Host recovery calls this only after verifying all old processes have stopped.
    /// No tool/network intent is replayed. Pins stay until separately verified.
    public func markInterruptedAfterProcessVerification() throws {
        try database.transaction {
            try database.execute("UPDATE messages SET status='interrupted' WHERE status='sending'; UPDATE turns SET state='interrupted' WHERE state IN ('pending','running'); UPDATE tasks SET state='interrupted' WHERE state IN ('approved','running'); DELETE FROM active_slot;")
            try database.execute("UPDATE approvals SET revoked_at=unixepoch() WHERE revoked_at IS NULL; UPDATE disclosures SET revoked_at=unixepoch() WHERE revoked_at IS NULL;")
            try appendEvent(type: "recovery.interrupted", payload: "{}")
        }
    }

    // Migration SQL is compiled, trusted host code, never a guest or UI input.
    // Internal until a real v2 migration is defined. Tests exercise the same mechanism.
    func migrate(to version: Int, sql: String, backupURL: URL) throws {
        guard version > WorkspaceSchema.version, !database.readOnly else { throw WorkspaceStoreError("Invalid migration target") }
        guard try database.query("SELECT 1 FROM active_slot").isEmpty else { throw WorkspaceStoreError("Migration requires no active turn") }
        let attributes = try FileManager.default.attributesOfFileSystem(forPath: URL(fileURLWithPath: database.path).deletingLastPathComponent().path)
        let size = (try FileManager.default.attributesOfItem(atPath: database.path)[.size] as? NSNumber)?.uint64Value ?? 0
        let walSize = (try? FileManager.default.attributesOfItem(atPath: database.path + "-wal")[.size] as? NSNumber)?.uint64Value ?? 0
        guard let free = attributes[.systemFreeSize] as? NSNumber, free.uint64Value > (size + walSize) * 3 + 1_048_576 else { throw WorkspaceStoreError("Insufficient migration disk space") }
        try database.backup(to: backupURL.path)
        let jobID = UUID().uuidString
        try database.query("INSERT INTO maintenance_jobs VALUES(?,'migration','backed-up',?,'running')", [jobID, backupURL.lastPathComponent])
        do {
            try database.transaction {
                try database.execute(sql)
                try database.query("UPDATE workspaces SET schema_version=? WHERE id=1", [String(version)])
                try database.execute("PRAGMA user_version=\(version)")
                try database.query("UPDATE maintenance_jobs SET phase='committed',state='complete' WHERE id=?", [jobID])
                try appendEvent(type: "schema.migrated", payload: "{}")
            }
        } catch {
            maintenanceError = "Migration failed; old schema retained, restore backup before execution"
            try database.becomeReadOnly()
            throw error
        }
    }
}
