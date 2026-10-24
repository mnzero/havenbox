import Foundation
import CSQLite
@testable import HavenboxCore

func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw WorkspaceStoreError(message) }
}
func rejects(_ label: String, _ body: () throws -> Void) throws {
    do { try body() } catch { print("PASS reject: \(label)"); return }
    throw WorkspaceStoreError("Expected rejection: \(label)")
}

extension WorkspaceStore {
    func constraints(turn: String) throws {
        try require(try database.query("PRAGMA foreign_keys")[0][0] == "1", "foreign keys enabled")
        try require(try database.query("PRAGMA journal_mode")[0][0] == "wal", "WAL enabled")
        try require(try database.query("PRAGMA synchronous")[0][0] == "2", "FULL enabled")
        try rejects("FK") { try database.query("INSERT INTO drafts VALUES('missing','x',0)") }
        try rejects("binding CHECK") { try database.execute("INSERT INTO sessions VALUES('bad','bad','agent',NULL,NULL,'ready',0)") }
        try rejects("execution needs task") { try database.query("INSERT INTO approvals VALUES('bad',?,NULL,'execution','digest',1,999,NULL)", [turn]) }
        try rejects("task needs matching plan") { try database.query("INSERT INTO tasks VALUES('bad',?,'p',1,'wrong','{}','approved')", [turn]) }
        try rejects("approval needs matching digest") { try database.query("INSERT INTO approvals SELECT 'bad',turn_id,id,'execution','wrong',1,999,NULL FROM tasks LIMIT 1") }
        try rejects("turn UNIQUE") { try database.query("INSERT INTO turns SELECT 'bad',session_id,seq,'new',NULL,'{}','pending' FROM turns LIMIT 1") }
        try rejects("active singleton CHECK") { try database.query("INSERT INTO active_slot VALUES(2,?,1,'active')", [turn]) }
        let before = try database.query("SELECT count(*) FROM events")
        try rejects("state and event rollback") {
            try database.transaction {
                try database.execute("UPDATE sessions SET title='should rollback' WHERE id='s'")
                try appendEvent(type: "must.rollback", payload: "{}")
                try database.execute("INSERT INTO active_slot VALUES(1,'missing',1,'active')")
            }
        }
        try require(try database.query("SELECT count(*) FROM events") == before, "no partial event")
        try require(try database.query("SELECT title FROM sessions WHERE id='s'")[0][0] == "source", "no partial state")
        try database.execute("INSERT INTO provenance VALUES('prov','tool1','engine1','rules','ids+hashes',0)")
        try database.query("INSERT INTO file_versions VALUES('f','artifact','objects/f',?,2,'complete','live','prov')", [String(repeating: "a", count: 64)])
        try database.execute("INSERT INTO file_refs VALUES('target','f','s','shared'); INSERT INTO file_pins VALUES('pin','f','preview','preview1',1,'active')")
        try rejects("referenced file deletion") { try database.execute("DELETE FROM file_versions WHERE id='f'") }
        try database.execute("INSERT INTO file_versions SELECT 'dead','artifact','objects/dead',sha256,0,'complete','deleting','prov' FROM file_versions WHERE id='f'")
        try rejects("deleting file pin") { try database.execute("INSERT INTO file_pins VALUES('bad','dead','preview','p',1,'active')") }
        try rejects("deleting file reference") { try database.execute("INSERT INTO file_refs VALUES('target','dead',NULL,'input')") }
        print("PASS SQL constraints and rollback")
    }

    func deleteSourceAndCheck() throws {
        try database.transaction {
            try database.execute("DELETE FROM sessions WHERE id='s'")
            try appendEvent(type: "session.deleted", payload: "{}")
        }
        try require(try database.query("SELECT source_session_id FROM file_refs WHERE session_id='target'") == [[nil]], "SET NULL retains target")
        try require(try database.query("SELECT provenance_id FROM file_versions WHERE id='f'") == [["prov"]], "provenance survives")
        try require(try database.query("SELECT count(*) FROM tasks")[0][0] == "0", "source task history deleted")
        try require(try database.query("PRAGMA foreign_key_check").isEmpty, "foreign key integrity")
        print("PASS source deletion preserves target and provenance")
    }

    func failMigration(backup: URL) throws {
        try rejects("migration rollback") { try migrate(to: 3, sql: "CREATE TABLE should_rollback(id TEXT); INSERT INTO absent VALUES(1);", backupURL: backup) }
        try require(isReadOnly, "migration failure becomes read-only")
        try require(try database.query("PRAGMA user_version")[0][0] == "2", "old schema retained")
        try require(try database.query("SELECT name FROM sqlite_master WHERE name='should_rollback'").isEmpty, "DDL rolled back")
        try rejects("read-only database connection") { try database.execute("UPDATE sessions SET title='bad'") }
    }

    func crashMidTransaction() throws {
        try database.execute("BEGIN IMMEDIATE; UPDATE sessions SET title='crashed' WHERE id='s';")
        try appendEvent(type: "crashed.event", payload: "{}")
        // Deliberately skip Swift deinit and SQL rollback, simulating abrupt host death.
        _exit(77)
    }

    func verifyCrashRollback(expectedEvents: Int) throws {
        try require(try database.query("SELECT title FROM sessions WHERE id='s'") == [["source"]], "crash rolls back state")
        try require(try database.query("SELECT count(*) FROM events")[0][0] == String(expectedEvents), "crash rolls back event")
    }

    func verifyDisclosure(turn: String) throws {
        try require(try database.query("SELECT task_id FROM approvals WHERE turn_id=? AND kind='disclosure'", [turn]) == [[nil]], "first disclosure without task")
        try require(try database.query("SELECT count(*) FROM tasks")[0][0] == "0", "no task before plan approval")
    }

    func rejectActiveMigration(backup: URL) throws {
        try rejects("active turn blocks migration") { try migrate(to: 3, sql: "CREATE TABLE never_created(id);", backupURL: backup) }
        try require(!FileManager.default.fileExists(atPath: backup.path), "active migration does not create backup")
    }

    func integrity() throws {
        try require(try database.query("PRAGMA integrity_check") == [["ok"]], "integrity check")
        try require(try database.query("PRAGMA foreign_key_check").isEmpty, "foreign key check")
    }
}

@main struct StorageTests {
    static func migrationRegression(root: URL) async throws {
        for fail in [false, true] {
            let directory = root.appendingPathComponent(fail ? "v1-failed" : "v1-upgrade")
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            do {
                guard let resolved = realpath(directory.path, nil) else { throw WorkspaceStoreError("fixture path") }
                defer { free(resolved) }
                let db = try WorkspaceDatabase(path: String(cString: resolved) + "/workspace.db")
                let v1 = WorkspaceSchema.sql.replacingOccurrences(of: "sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE", with: "id TEXT PRIMARY KEY")
                try db.execute(v1 + WorkspaceSchema.referenceGuards)
                try db.execute("UPDATE workspaces SET schema_version=1; PRAGMA user_version=1;")
                try db.execute("INSERT INTO compatibility_checks VALUES('old-pass','legacy','suite','passed','first'); INSERT INTO compatibility_checks VALUES('old-fail','legacy','suite','failed','second');")
                if fail { try db.execute("CREATE TRIGGER block_migration BEFORE UPDATE ON workspaces BEGIN SELECT RAISE(ABORT,'fixture failure'); END;") }
            }
            var store: WorkspaceStore? = try WorkspaceStore(directory: directory)
            if fail {
                try require(await store!.isReadOnly, "v1 migration failure is read-only")
                try require(try await store!.readEvents(epoch: 1).isEmpty, "history readable after failure")
                store = nil
                store = try WorkspaceStore(directory: directory)
                try require(await store!.isReadOnly, "failed v1 migration remains read-only on reopen")
            } else {
                try require(await !store!.isReadOnly, "v1 upgrade writable")
                try require(try await store!.compatibilityCheck(digest: "legacy", suiteVersion: "suite")?.status == "failed", "v1 rows retain insertion order")
                try await store!.saveCompatibilityCheck(CompatibilityCheck(digest: "legacy", status: "passed", suiteVersion: "suite", evidenceReference: "new"))
                store = nil
                store = try WorkspaceStore(directory: directory)
                try require(try await store!.compatibilityCheck(digest: "legacy", suiteVersion: "suite")?.evidenceReference == "new", "migrated sequence advances and survives reopen")
                try await store!.integrity()
            }
        }
        print("PASS v1 real SQLite migration, evidence ordering, reopen and failure read-only")
    }

    static func main() async throws {
        if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--second-writer" {
            do { _ = try WorkspaceStore(directory: URL(fileURLWithPath: CommandLine.arguments[2])); exit(1) }
            catch { print("second writer rejected: \(error)"); exit(0) }
        }
        if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--crash-transaction" {
            let store = try WorkspaceStore(directory: URL(fileURLWithPath: CommandLine.arguments[2]))
            try await store.crashMidTransaction()
            exit(1)
        }
        let root = FileManager.default.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("hb10-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        print("SQLite \(String(cString: sqlite3_libversion())) source \(String(cString: sqlite3_sourceid()))")
        print("Persistent test artifacts: \(root.path)")
        try await migrationRegression(root: root)
        let workspace = root.appendingPathComponent("live")
        var store: WorkspaceStore? = try WorkspaceStore(directory: workspace)
        try await store!.createSession(id: "s", title: "source", recipeRevision: "r1", toolPolicyRevision: nil)
        try await store!.createSession(id: "target", title: "target", recipeRevision: nil, toolPolicyRevision: "tools1")
        let messageBody = "hello ' SQL\u{0000} trailing Unicode 测试"
        let turn = try await store!.sendMessage(sessionID: "s", commandID: "send-1", body: messageBody)
        let duplicate = try await store!.sendMessage(sessionID: "s", commandID: "send-1", body: messageBody)
        try require(try await store!.readHistory(sessionID: "s")[0].body == messageBody, "NUL and Unicode round trip")
        try require(turn == duplicate, "double click dedupe")
        do { _ = try await store!.sendMessage(sessionID: "target", commandID: "send-2", body: "second"); throw WorkspaceStoreError("accepted second active turn") }
        catch { try require(String(describing: error) != "accepted second active turn", "active slot") }
        _ = try await store!.recordDisclosure(turnID: turn, digest: "first-disclosure", expiresAt: 9999999999)
        try await store!.verifyDisclosure(turn: turn)
        try await store!.rejectActiveMigration(backup: root.appendingPathComponent("must-not-exist.db"))
        try await store!.savePlan(id: "p", revision: 1, turnID: turn, canonicalJSON: "{}", digest: "plan-digest")
        let task = try await store!.approveExecution(commandID: "approve-1", turnID: turn, planID: "p", revision: 1, digest: "plan-digest", snapshotJSON: "{}", expiresAt: 9999999999)
        let duplicateTask = try await store!.approveExecution(commandID: "approve-1", turnID: turn, planID: "p", revision: 1, digest: "plan-digest", snapshotJSON: "{}", expiresAt: 9999999999)
        try require(task == duplicateTask, "execution command dedupe")
        try await store!.constraints(turn: turn)
        let child = Process()
        child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        child.arguments = ["--second-writer", workspace.path]
        try child.run(); child.waitUntilExit()
        try require(child.terminationStatus == 0, "second process cannot write")
        let backupDirectory = root.appendingPathComponent("restored")
        try FileManager.default.createDirectory(at: backupDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let wal = try FileManager.default.attributesOfItem(atPath: workspace.appendingPathComponent("workspace.db-wal").path)
        try require((wal[.size] as! NSNumber).intValue > 0, "live WAL contains writes")
        try await store!.backup(to: backupDirectory.appendingPathComponent("workspace.db"))
        let restored = try WorkspaceStore(directory: backupDirectory)
        try require(try await restored.readHistory(sessionID: "s").count == 1, "WAL backup preserves message")
        try await restored.integrity()
        let events = try await store!.readEvents(epoch: 1)
        try require(events.map(\.sequence) == Array(1...Int64(events.count)), "stable gap-free committed event sequence")
        let tail = try await store!.readEvents(epoch: 1, after: events.last!.sequence)
        try require(tail.isEmpty, "event reconnect cursor")
        store = nil
        let crash = Process()
        crash.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        crash.arguments = ["--crash-transaction", workspace.path]
        try crash.run(); crash.waitUntilExit()
        try require(crash.terminationStatus == 77, "crash occurred inside transaction")
        store = try WorkspaceStore(directory: workspace)
        try await store!.verifyCrashRollback(expectedEvents: events.count)
        let reconnected = try await store!.sendMessage(sessionID: "s", commandID: "send-1", body: messageBody)
        try require(reconnected == turn, "reopen command dedupe")
        try require(try await store!.readHistory(sessionID: "s").count == 1, "reopen no duplicate messages")
        try await store!.markInterruptedAfterProcessVerification()
        try await store!.deleteSourceAndCheck()
        let migrationBackup = root.appendingPathComponent("migration-backup.db")
        try await store!.failMigration(backup: migrationBackup)
        store = nil
        store = try WorkspaceStore(directory: workspace)
        try require(await store!.isReadOnly, "failed migration remains read-only after restart")
        let recoveryDirectory = root.appendingPathComponent("migration-recovery")
        try FileManager.default.createDirectory(at: recoveryDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try FileManager.default.copyItem(at: migrationBackup, to: recoveryDirectory.appendingPathComponent("workspace.db"))
        let recovered = try WorkspaceStore(directory: recoveryDirectory)
        try require(await !recovered.isReadOnly, "old backup can reopen writable")
        try await recovered.integrity()
        try await recovered.createSession(id: "after-restore", title: "restored", recipeRevision: nil, toolPolicyRevision: "tools")
        try await recovered.checkpoint()
        print("PASS HB10 real SQLite storage regressions")
    }
}
