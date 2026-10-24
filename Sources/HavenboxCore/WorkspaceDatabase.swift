import Foundation
import CSQLite
import Darwin

public struct WorkspaceStoreError: Error, CustomStringConvertible, Sendable {
    public let description: String
    init(_ message: String) { description = message }
}

// Confined to WorkspaceStore's actor. Never handed to UI or guest code.
final class WorkspaceDatabase {
    private var handle: OpaquePointer?
    private var lockFD: Int32 = -1
    private(set) var readOnly = false
    let path: String

    init(path: String) throws {
        self.path = path
        guard sqlite3_libversion_number() == 3_051_003 else {
            throw WorkspaceStoreError("Unpinned SQLite runtime")
        }
        // A separate flock survives short SQL transactions and rejects a second host instance.
        lockFD = Darwin.open(path + ".writer-lock", O_CREAT | O_RDWR | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard lockFD >= 0 else { throw WorkspaceStoreError("Cannot open workspace writer lock") }
        guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            close(lockFD); lockFD = -1
            throw WorkspaceStoreError("Workspace already has a writer")
        }
        do {
            var info = stat()
            if lstat(path, &info) == 0 && (info.st_mode & S_IFMT) != S_IFREG {
                throw WorkspaceStoreError("Database must be a regular file")
            }
            try open(readOnly: false)
            try execute("PRAGMA foreign_keys=ON; PRAGMA trusted_schema=OFF; PRAGMA synchronous=FULL; PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;")
        } catch {
            if let handle { sqlite3_close_v2(handle); self.handle = nil }
            close(lockFD); lockFD = -1
            throw error
        }
    }

    deinit {
        if let handle { sqlite3_close_v2(handle) }
        if lockFD >= 0 { close(lockFD) }
    }

    private func open(readOnly: Bool) throws {
        let flags = readOnly ? SQLITE_OPEN_READONLY : SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE
        guard sqlite3_open_v2(path, &handle, flags | SQLITE_OPEN_FULLMUTEX | SQLITE_OPEN_NOFOLLOW, nil) == SQLITE_OK else {
            throw failure("open")
        }
        sqlite3_extended_result_codes(handle, 1)
        sqlite3_busy_timeout(handle, 1000)
        if !readOnly && chmod(path, S_IRUSR | S_IWUSR) != 0 { throw WorkspaceStoreError("Cannot restrict database permissions") }
    }

    func becomeReadOnly() throws {
        guard sqlite3_close(handle) == SQLITE_OK else { throw failure("close for read-only") }
        handle = nil
        readOnly = true
        try open(readOnly: true)
        try execute("PRAGMA foreign_keys=ON; PRAGMA query_only=ON; PRAGMA trusted_schema=OFF;")
    }

    private func failure(_ operation: String) -> WorkspaceStoreError {
        // Deliberately omit SQL, bindings and engine messages that can expose user content.
        WorkspaceStoreError("SQLite \(operation) failed (code \(sqlite3_extended_errcode(handle)))")
    }

    func execute(_ sql: String) throws {
        guard sqlite3_exec(handle, sql, nil, nil, nil) == SQLITE_OK else { throw failure("execute") }
    }

    @discardableResult
    func query(_ sql: String, _ bindings: [String?] = []) throws -> [[String?]] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK else { throw failure("prepare") }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_bind_parameter_count(statement) == bindings.count else { throw WorkspaceStoreError("Binding count mismatch") }
        for (index, value) in bindings.enumerated() {
            let rc: Int32
            if let value {
                guard value.utf8.count <= Int(Int32.max) else { throw WorkspaceStoreError("Text exceeds SQLite binding limit") }
                rc = value.withCString { sqlite3_bind_text(statement, Int32(index + 1), $0, Int32(value.utf8.count), unsafeBitCast(-1, to: sqlite3_destructor_type.self)) }
            } else { rc = sqlite3_bind_null(statement, Int32(index + 1)) }
            guard rc == SQLITE_OK else { throw failure("bind") }
        }
        var rows: [[String?]] = []
        while true {
            let rc = sqlite3_step(statement)
            if rc == SQLITE_DONE { return rows }
            guard rc == SQLITE_ROW else { throw failure("step") }
            rows.append((0..<sqlite3_column_count(statement)).map { column in
                guard let value = sqlite3_column_text(statement, column) else { return nil }
                let count = Int(sqlite3_column_bytes(statement, column))
                return String(decoding: UnsafeBufferPointer(start: value, count: count), as: UTF8.self)
            })
        }
    }

    func transaction<T>(_ body: () throws -> T) throws -> T {
        guard !readOnly else { throw WorkspaceStoreError("Workspace is read-only") }
        try execute("BEGIN IMMEDIATE")
        do {
            let result = try body()
            try execute("COMMIT")
            return result
        } catch {
            let original = error
            do { try execute("ROLLBACK") }
            catch { throw WorkspaceStoreError("Rollback failed; close workspace: \(error)") }
            throw original
        }
    }

    func backup(to destination: String) throws {
        let url = URL(fileURLWithPath: destination)
        guard let parent = realpath(url.deletingLastPathComponent().path, nil) else { throw WorkspaceStoreError("Cannot resolve backup directory") }
        defer { free(parent) }
        let destination = String(cString: parent) + "/" + url.lastPathComponent
        // Reserve a new file; never overwrite a user's existing backup.
        let fd = Darwin.open(destination, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw WorkspaceStoreError("Backup destination must be new") }
        close(fd)
        var target: OpaquePointer?
        guard sqlite3_open_v2(destination, &target, SQLITE_OPEN_READWRITE | SQLITE_OPEN_NOFOLLOW, nil) == SQLITE_OK else {
            if let target { sqlite3_close_v2(target) }
            throw WorkspaceStoreError("Cannot open backup")
        }
        defer { sqlite3_close_v2(target) }
        guard let backup = sqlite3_backup_init(target, "main", handle, "main") else { throw failure("backup init") }
        let step = sqlite3_backup_step(backup, -1)
        let finish = sqlite3_backup_finish(backup)
        guard step == SQLITE_DONE && finish == SQLITE_OK else { throw WorkspaceStoreError("Backup incomplete (\(step), \(finish)); do not restore") }
    }

    func checkpoint() throws {
        guard !readOnly else { throw WorkspaceStoreError("Workspace is read-only") }
        guard sqlite3_wal_checkpoint_v2(handle, nil, SQLITE_CHECKPOINT_TRUNCATE, nil, nil) == SQLITE_OK else { throw failure("checkpoint") }
    }
}
