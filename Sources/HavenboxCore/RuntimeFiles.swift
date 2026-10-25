import CryptoKit
import Darwin
import Foundation

public struct RuntimeFileIdentity: Equatable, Hashable, Sendable {
    public let device: UInt64
    public let inode: UInt64
}
public final class RuntimeOpenedFile {
    public enum Requirement {
        case immutable(sha256: String, executable: Bool)
        case writableData
        case configuration(maximumBytes: Int)
        case importedInput(maximumBytes: Int)
        case stagedOutput(maximumBytes: Int)
    }

    public let path: String
    public let identity: RuntimeFileIdentity
    public var byteCount: Int64 { metadata.size }
    private let descriptor: Int32
    private let metadata: Metadata
    private let requirement: Requirement

    private struct Metadata: Equatable {
        let device: UInt64
        let inode: UInt64
        let mode: UInt16
        let owner: UInt32
        let links: UInt16
        let size: Int64
        let modifiedSeconds: Int
        let modifiedNanoseconds: Int

        init(_ value: stat) {
            device = UInt64(value.st_dev)
            inode = UInt64(value.st_ino)
            mode = UInt16(value.st_mode)
            owner = value.st_uid
            links = UInt16(value.st_nlink)
            size = value.st_size
            modifiedSeconds = value.st_mtimespec.tv_sec
            modifiedNanoseconds = value.st_mtimespec.tv_nsec
        }
    }

    private init(path: String, descriptor: Int32, metadata: Metadata, requirement: Requirement) {
        self.path = path
        self.descriptor = descriptor
        self.metadata = metadata
        self.requirement = requirement
        identity = RuntimeFileIdentity(device: metadata.device, inode: metadata.inode)
    }

    deinit { Darwin.close(descriptor) }

    public static func open(path: String, requirement: Requirement) throws -> RuntimeOpenedFile {
        let standardized = URL(fileURLWithPath: path).standardizedFileURL.path
        guard path.hasPrefix("/"), path == standardized, path != "/",
              !path.contains(","), !path.contains("\n") else {
            throw RuntimeError.assetRejected("noncanonical file path")
        }
        let components = path.split(separator: "/", omittingEmptySubsequences: false)
        guard components.first == "", components.dropFirst().allSatisfy({ !$0.isEmpty }) else {
            throw RuntimeError.assetRejected("noncanonical file path")
        }
        var directory = Darwin.open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard directory >= 0 else { throw RuntimeError.system(errno) }
        defer { if directory >= 0 { Darwin.close(directory) } }
        for component in components.dropFirst().dropLast() {
            let next = component.withCString {
                Darwin.openat(directory, $0, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            }
            guard next >= 0 else { throw RuntimeError.assetRejected("file parent rejected") }
            var status = stat()
            let requiresProtectedParent: Bool
            switch requirement {
            case .importedInput, .stagedOutput: requiresProtectedParent = false
            default: requiresProtectedParent = true
            }
            guard fstat(next, &status) == 0,
                  status.st_mode & S_IFMT == S_IFDIR,
                  (!requiresProtectedParent || status.st_mode & 0o022 == 0) else {
                Darwin.close(next)
                throw RuntimeError.assetRejected("writable or invalid file parent")
            }
            Darwin.close(directory)
            directory = next
        }
        let flags: Int32
        switch requirement {
        case .writableData: flags = O_RDWR | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK
        default: flags = O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK
        }
        let leaf = String(components.last!)
        let descriptor = leaf.withCString { Darwin.openat(directory, $0, flags) }
        guard descriptor >= 0 else { throw RuntimeError.assetRejected("file open rejected") }
        var owner: RuntimeOpenedFile?
        do {
            let before = try checkedMetadata(descriptor)
            try validate(before, requirement: requirement)
            let opened = RuntimeOpenedFile(path: path, descriptor: descriptor, metadata: before,
                                           requirement: requirement)
            owner = opened
            switch requirement {
            case let .immutable(expected, _):
                guard try opened.sha256() == expected else {
                    throw RuntimeError.assetRejected("asset digest mismatch")
                }
            case let .configuration(maximumBytes):
                _ = try opened.readAll(maximumBytes: maximumBytes)
            case .writableData, .importedInput, .stagedOutput:
                break
            }
            try opened.requireUnchanged()
            return opened
        } catch {
            if owner == nil { Darwin.close(descriptor) }
            throw error
        }
    }

    public func readAll(maximumBytes: Int) throws -> Data {
        guard maximumBytes >= 0, metadata.size >= 0, metadata.size <= maximumBytes else {
            throw RuntimeError.invalidConfiguration
        }
        var data = Data(count: Int(metadata.size))
        var offset = 0
        while offset < data.count {
            let remaining = data.count - offset
            let count = data.withUnsafeMutableBytes { bytes in
                pread(descriptor, bytes.baseAddress! + offset, remaining, off_t(offset))
            }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else { throw RuntimeError.invalidConfiguration }
            offset += count
        }
        var extra: UInt8 = 0
        let extraCount = pread(descriptor, &extra, 1, off_t(offset))
        guard extraCount == 0 else { throw RuntimeError.invalidConfiguration }
        try requireUnchanged()
        return data
    }

    public func readChunk(offset: Int64, maximumBytes: Int) throws -> Data {
        guard offset >= 0, maximumBytes > 0, offset <= metadata.size else {
            throw RuntimeError.assetRejected("invalid file chunk range")
        }
        let wanted = min(Int64(maximumBytes), metadata.size - offset)
        var data = Data(count: Int(wanted))
        var completed = 0
        while completed < data.count {
            let remaining = data.count - completed
            let count = data.withUnsafeMutableBytes { bytes in
                pread(descriptor, bytes.baseAddress! + completed, remaining,
                      off_t(offset + Int64(completed)))
            }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else { throw RuntimeError.assetRejected("file changed while reading") }
            completed += count
        }
        try requireUnchanged()
        return data
    }

    public func sha256() throws -> String {
        guard metadata.size >= 0 else { throw RuntimeError.assetRejected("negative asset size") }
        var hasher = SHA256()
        var offset: Int64 = 0
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while offset < metadata.size {
            let wanted = min(buffer.count, Int(metadata.size - offset))
            let count = buffer.withUnsafeMutableBytes {
                pread(descriptor, $0.baseAddress, wanted, off_t(offset))
            }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else { throw RuntimeError.assetRejected("asset changed while hashing") }
            hasher.update(data: Data(buffer[..<count]))
            offset += Int64(count)
        }
        var extra: UInt8 = 0
        guard pread(descriptor, &extra, 1, off_t(offset)) == 0 else {
            throw RuntimeError.assetRejected("asset changed while hashing")
        }
        try requireUnchanged()
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    public func requireUnchanged() throws {
        guard try Self.checkedMetadata(descriptor) == metadata else {
            throw RuntimeError.assetRejected("opened file metadata changed")
        }
    }

    public func requirePathBindingUnchanged() throws {
        let current = try Self.open(path: path, requirement: requirement)
        guard current.identity == identity else {
            throw RuntimeError.assetRejected("file path binding changed")
        }
        try requireUnchanged()
    }

    private static func checkedMetadata(_ descriptor: Int32) throws -> Metadata {
        var status = stat()
        guard fstat(descriptor, &status) == 0 else { throw RuntimeError.system(errno) }
        return Metadata(status)
    }

    private static func validate(_ metadata: Metadata, requirement: Requirement) throws {
        guard metadata.mode & UInt16(S_IFMT) == UInt16(S_IFREG), metadata.links == 1 else {
            throw RuntimeError.assetRejected("file type or link count rejected")
        }
        switch requirement {
        case let .immutable(digest, executable):
            let ownerMayWrite = metadata.owner == 0 && getuid() != 0
            guard digest.count == 64,
                  digest.allSatisfy({ $0.isHexDigit && !$0.isUppercase }),
                  metadata.owner == 0 || metadata.owner == getuid(),
                  metadata.mode & 0o022 == 0,
                  ownerMayWrite || metadata.mode & 0o200 == 0,
                  !executable || metadata.mode & 0o111 != 0 else {
                throw RuntimeError.assetRejected("asset owner, mode, or digest rejected")
            }
        case .writableData:
            guard metadata.owner == getuid(), metadata.mode & 0o777 == 0o600 else {
                throw RuntimeError.invalidConfiguration
            }
        case let .configuration(maximumBytes):
            guard maximumBytes > 0, metadata.owner == getuid(),
                  metadata.mode & 0o777 == 0o600,
                  metadata.size >= 0, metadata.size <= maximumBytes else {
                throw RuntimeError.invalidConfiguration
            }
        case let .importedInput(maximumBytes):
            guard maximumBytes > 0, metadata.owner == getuid(),
                  metadata.size >= 0, metadata.size <= maximumBytes,
                  metadata.mode & 0o111 == 0 else {
                throw RuntimeError.assetRejected("input owner, mode, or size rejected")
            }
        case let .stagedOutput(maximumBytes):
            guard maximumBytes > 0, metadata.owner == getuid(),
                  metadata.size >= 0, metadata.size <= maximumBytes,
                  metadata.mode & 0o111 == 0 else {
                throw RuntimeError.assetRejected("output owner, mode, or size rejected")
            }
        }
    }
}
public enum HostFileMediaType: String, Codable, Sendable {
    case csv = "text/csv"
    case markdown = "text/markdown"

    fileprivate var pathExtension: String {
        switch self {
        case .csv: "csv"
        case .markdown: "md"
        }
    }
}

public struct HostFileVersion: Codable, Equatable, Sendable {
    public let fileID: String
    public let sha256: String
    public let size: UInt64
    public let mediaType: HostFileMediaType

    public init(fileID: String, sha256: String, size: UInt64, mediaType: HostFileMediaType) throws {
        _ = try FileVersionID(fileID)
        guard Self.validDigest(sha256) else { throw FileBoundaryError.invalidDigest }
        self.fileID = fileID
        self.sha256 = sha256
        self.size = size
        self.mediaType = mediaType
    }

    fileprivate static func validDigest(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy {
            (48...57).contains($0) || (97...102).contains($0)
        }
    }
}

public enum FileBoundaryError: Error, Equatable {
    case invalidPath
    case invalidMediaType
    case invalidDigest
    case sizeLimit
    case transferOrder
    case duplicate
    case contentMismatch
    case invalidText
    case unavailable
    case system(Int32)
}

private final class RuntimeDirectoryBinding: @unchecked Sendable {
    let path: String
    let identity: RuntimeFileIdentity
    private let descriptor: Int32

    init(path: String) throws {
        let descriptor = Darwin.open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw FileBoundaryError.invalidPath }
        var status = stat()
        guard fstat(descriptor, &status) == 0,
              status.st_mode & S_IFMT == S_IFDIR,
              status.st_uid == getuid(), status.st_mode & 0o077 == 0 else {
            Darwin.close(descriptor)
            throw FileBoundaryError.invalidPath
        }
        self.path = path
        self.descriptor = descriptor
        identity = RuntimeFileIdentity(device: UInt64(status.st_dev), inode: UInt64(status.st_ino))
    }

    deinit { Darwin.close(descriptor) }

    func requirePathBindingUnchanged() throws {
        let current = Darwin.open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard current >= 0 else { throw FileBoundaryError.invalidPath }
        defer { Darwin.close(current) }
        var status = stat()
        guard fstat(current, &status) == 0,
              RuntimeFileIdentity(device: UInt64(status.st_dev), inode: UInt64(status.st_ino)) == identity else {
            throw FileBoundaryError.invalidPath
        }
    }

    func createExclusive(_ name: String) throws -> Int32 {
        try requirePathBindingUnchanged()
        let result = name.withCString {
            Darwin.openat(descriptor, $0, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        }
        guard result >= 0 else {
            if errno == EEXIST { throw FileBoundaryError.duplicate }
            throw FileBoundaryError.system(errno)
        }
        return result
    }

    func renameExclusive(_ sourceName: String, to destination: RuntimeDirectoryBinding,
                         destinationName: String) throws {
        try requirePathBindingUnchanged()
        try destination.requirePathBindingUnchanged()
        let flags = UInt32(RENAME_EXCL | RENAME_NOFOLLOW_ANY | RENAME_RESOLVE_BENEATH)
        let result = sourceName.withCString { source in
            destinationName.withCString { target in
                renameatx_np(descriptor, source, destination.descriptor, target, flags)
            }
        }
        guard result == 0 else {
            if errno == EEXIST { throw FileBoundaryError.duplicate }
            throw FileBoundaryError.system(errno)
        }
        try requirePathBindingUnchanged()
        try destination.requirePathBindingUnchanged()
    }

    func sync() throws {
        try requirePathBindingUnchanged()
        guard fsync(descriptor) == 0 else { throw FileBoundaryError.system(errno) }
    }
}

public struct HostInputChunk: Equatable, Sendable {
    public let frame: RuntimeFrame
    public let nextOffset: UInt64
    public let isFinal: Bool
}

private struct BeginInputPayload: Encodable {
    let type = RuntimeMessageKind.beginInput.rawValue
    let fileId: String
    let size: String
    let sha256: String
    let mediaType: String
}

private struct FileChunkPayload: Codable {
    let type: String
    let fileId: String
    let offset: String
    let bytes: Data
}

private struct CommitInputPayload: Encodable {
    let type = RuntimeMessageKind.commitInput.rawValue
    let fileId: String
}

private final class PendingHostOutput: @unchecked Sendable {
    private(set) var descriptor: Int32
    let path: String
    let version: HostFileVersion
    let taskID: String
    let identity: RuntimeFileIdentity
    var written: UInt64 = 0
    var published = false

    init(descriptor: Int32, path: String, version: HostFileVersion, taskID: String) {
        self.descriptor = descriptor
        self.path = path
        self.version = version
        self.taskID = taskID
        var status = stat()
        precondition(fstat(descriptor, &status) == 0)
        identity = RuntimeFileIdentity(device: UInt64(status.st_dev), inode: UInt64(status.st_ino))
    }

    deinit {
        close()
        if !published { HostFileBoundary.removeIfIdentityMatches(path: path, identity: identity) }
    }

    func close() {
        if descriptor >= 0 {
            Darwin.close(descriptor)
            descriptor = -1
        }
    }
}

/// Trusted host-side file boundary. It never passes a host path over the runtime protocol.
public actor HostFileBoundary {
    public static let defaultMaximumFileBytes = 50 * 1024 * 1024
    public static let defaultChunkBytes = 128 * 1024
    public static let maximumFileChunkBytes = 190 * 1024

    private let store: WorkspaceStore
    private let inputDirectory: URL
    private let stagingDirectory: URL
    private let artifactDirectory: URL
    private let inputDirectoryBinding: RuntimeDirectoryBinding
    private let stagingDirectoryBinding: RuntimeDirectoryBinding
    private let artifactDirectoryBinding: RuntimeDirectoryBinding
    private let maximumInputBytes: Int
    private let maximumOutputBytes: Int
    private let chunkBytes: Int
    private var pendingOutputs: [String: PendingHostOutput] = [:]

    public init(root: URL, store: WorkspaceStore,
                maximumInputBytes: Int = HostFileBoundary.defaultMaximumFileBytes,
                maximumOutputBytes: Int = HostFileBoundary.defaultMaximumFileBytes,
                chunkBytes: Int = HostFileBoundary.defaultChunkBytes) throws {
        guard maximumInputBytes > 0, maximumOutputBytes > 0,
              chunkBytes > 0, chunkBytes <= Self.maximumFileChunkBytes,
              root.isFileURL, root.path == root.standardizedFileURL.path else {
            throw FileBoundaryError.sizeLimit
        }
        self.store = store
        self.maximumInputBytes = maximumInputBytes
        self.maximumOutputBytes = maximumOutputBytes
        self.chunkBytes = chunkBytes
        let files = root.appendingPathComponent("files", isDirectory: true)
        inputDirectory = files.appendingPathComponent("input", isDirectory: true)
        stagingDirectory = files.appendingPathComponent("staging", isDirectory: true)
        artifactDirectory = files.appendingPathComponent("artifacts", isDirectory: true)
        for directory in [root, files, inputDirectory, stagingDirectory, artifactDirectory] {
            try Self.ensurePrivateDirectory(directory)
        }
        inputDirectoryBinding = try RuntimeDirectoryBinding(path: inputDirectory.path)
        stagingDirectoryBinding = try RuntimeDirectoryBinding(path: stagingDirectory.path)
        artifactDirectoryBinding = try RuntimeDirectoryBinding(path: artifactDirectory.path)
    }

    public func importCSV(from source: URL) async throws -> HostFileVersion {
        guard source.isFileURL, source.pathExtension.lowercased() == "csv",
              source.path == source.standardizedFileURL.path else {
            throw FileBoundaryError.invalidPath
        }
        let opened = try RuntimeOpenedFile.open(
            path: source.path, requirement: .importedInput(maximumBytes: maximumInputBytes)
        )
        let size = UInt64(opened.byteCount)
        let digest = try opened.sha256()
        let fileID = "file_" + UUID().uuidString.lowercased()
        let version = try HostFileVersion(fileID: fileID, sha256: digest, size: size, mediaType: .csv)
        let temporary = inputDirectory.appendingPathComponent(".\(fileID).part")
        let target = inputDirectory.appendingPathComponent(fileID)
        var temporaryIdentity: RuntimeFileIdentity?
        var installedIdentity: RuntimeFileIdentity?
        defer {
            if let temporaryIdentity {
                Self.removeIfIdentityMatches(path: temporary.path, identity: temporaryIdentity)
            }
            if let installedIdentity {
                Self.removeIfIdentityMatches(path: target.path, identity: installedIdentity)
            }
        }
        let output = try inputDirectoryBinding.createExclusive(temporary.lastPathComponent)
        temporaryIdentity = try Self.identity(of: output)
        do {
            try Self.copy(opened, to: output, chunkBytes: chunkBytes)
            guard fchmod(output, S_IRUSR) == 0, fsync(output) == 0 else {
                throw FileBoundaryError.system(errno)
            }
        } catch {
            Darwin.close(output)
            throw error
        }
        Darwin.close(output)
        try opened.requirePathBindingUnchanged()
        let staged = try RuntimeOpenedFile.open(
            path: temporary.path, requirement: .immutable(sha256: digest, executable: false)
        )
        let contents = try staged.readAll(maximumBytes: maximumInputBytes)
        try Self.validate(contents, mediaType: .csv)
        try inputDirectoryBinding.renameExclusive(
            temporary.lastPathComponent, to: inputDirectoryBinding,
            destinationName: target.lastPathComponent
        )
        installedIdentity = staged.identity
        let published = try RuntimeOpenedFile.open(
            path: target.path, requirement: .immutable(sha256: digest, executable: false)
        )
        guard UInt64(published.byteCount) == version.size else { throw FileBoundaryError.contentMismatch }
        try published.requirePathBindingUnchanged()
        try inputDirectoryBinding.sync()
        try await store.recordInputVersion(version, objectKey: "files/input/\(fileID)")
        installedIdentity = nil
        return version
    }

    public func beginInputFrame(for version: HostFileVersion, epoch: UInt64,
                                request: UUID) throws -> RuntimeFrame {
        let payload = try Self.encode(BeginInputPayload(
            fileId: version.fileID, size: String(version.size), sha256: version.sha256,
            mediaType: version.mediaType.rawValue
        ))
        return try RuntimeFrame(channel: .file, epoch: epoch, request: request,
                                payload: payload, direction: .hostToGuest)
    }

    public func inputChunk(for version: HostFileVersion, offset: UInt64,
                           epoch: UInt64, request: UUID) throws -> HostInputChunk {
        guard offset <= version.size, offset <= UInt64(Int64.max) else {
            throw FileBoundaryError.transferOrder
        }
        let path = inputDirectory.appendingPathComponent(version.fileID).path
        let opened = try RuntimeOpenedFile.open(
            path: path, requirement: .immutable(sha256: version.sha256, executable: false)
        )
        guard UInt64(opened.byteCount) == version.size else { throw FileBoundaryError.contentMismatch }
        let bytes = try opened.readChunk(offset: Int64(offset), maximumBytes: chunkBytes)
        let payload = try Self.encode(FileChunkPayload(
            type: RuntimeMessageKind.fileChunk.rawValue,
            fileId: version.fileID, offset: String(offset), bytes: bytes
        ))
        let frame = try RuntimeFrame(channel: .file, epoch: epoch, request: request,
                                     payload: payload, direction: .hostToGuest)
        let next = offset + UInt64(bytes.count)
        return HostInputChunk(frame: frame, nextOffset: next, isFinal: next == version.size)
    }

    public func commitInputFrame(for version: HostFileVersion, epoch: UInt64,
                                 request: UUID) throws -> RuntimeFrame {
        let payload = try Self.encode(CommitInputPayload(fileId: version.fileID))
        return try RuntimeFrame(channel: .file, epoch: epoch, request: request,
                                payload: payload, direction: .hostToGuest)
    }

    public func beginOutput(fileID: String, taskID: String, mediaType: HostFileMediaType,
                            size: UInt64, sha256: String) async throws {
        _ = try FileVersionID(fileID)
        guard size <= UInt64(maximumOutputBytes) else { throw FileBoundaryError.sizeLimit }
        let version = try HostFileVersion(fileID: fileID, sha256: sha256,
                                          size: size, mediaType: mediaType)
        guard pendingOutputs[fileID] == nil else { throw FileBoundaryError.duplicate }
        try await store.requireArtifactBegin(fileID: fileID, taskID: taskID)
        let path = stagingDirectory.appendingPathComponent(".\(fileID).part").path
        let descriptor = try stagingDirectoryBinding.createExclusive(".\(fileID).part")
        pendingOutputs[fileID] = PendingHostOutput(
            descriptor: descriptor, path: path, version: version, taskID: taskID
        )
    }

    public func receiveOutputChunk(fileID: String, offset: UInt64, bytes: Data) throws {
        guard let pending = pendingOutputs[fileID] else { throw FileBoundaryError.unavailable }
        guard !bytes.isEmpty, bytes.count <= chunkBytes, offset == pending.written,
              offset <= pending.version.size,
              UInt64(bytes.count) <= pending.version.size - offset else {
            throw FileBoundaryError.transferOrder
        }
        do {
            try Self.writeAll(bytes, descriptor: pending.descriptor)
            pending.written += UInt64(bytes.count)
        } catch {
            pendingOutputs.removeValue(forKey: fileID)
            throw error
        }
    }

    public func commitOutput(fileID: String) async throws -> HostFileVersion {
        guard let pending = pendingOutputs.removeValue(forKey: fileID) else {
            throw FileBoundaryError.unavailable
        }
        guard pending.written == pending.version.size else { throw FileBoundaryError.contentMismatch }
        guard fchmod(pending.descriptor, S_IRUSR) == 0, fsync(pending.descriptor) == 0 else {
            throw FileBoundaryError.system(errno)
        }
        pending.close()
        let opened = try RuntimeOpenedFile.open(
            path: pending.path,
            requirement: .immutable(sha256: pending.version.sha256, executable: false)
        )
        let contents = try opened.readAll(maximumBytes: maximumOutputBytes)
        try Self.validate(contents, mediaType: pending.version.mediaType)
        let target = artifactDirectory.appendingPathComponent(
            "\(fileID).\(pending.version.mediaType.pathExtension)"
        )
        var installedIdentity: RuntimeFileIdentity?
        defer {
            if let installedIdentity {
                Self.removeIfIdentityMatches(path: target.path, identity: installedIdentity)
            }
        }
        try opened.requirePathBindingUnchanged()
        try stagingDirectoryBinding.renameExclusive(
            URL(fileURLWithPath: pending.path).lastPathComponent, to: artifactDirectoryBinding,
            destinationName: target.lastPathComponent
        )
        pending.published = true
        installedIdentity = opened.identity
        let published = try RuntimeOpenedFile.open(
            path: target.path,
            requirement: .immutable(sha256: pending.version.sha256, executable: false)
        )
        guard UInt64(published.byteCount) == pending.version.size,
              published.identity == opened.identity else { throw FileBoundaryError.contentMismatch }
        try published.requirePathBindingUnchanged()
        try artifactDirectoryBinding.sync()
        try await store.recordArtifactVersion(
            pending.version, role: "artifact", objectKey: "files/artifacts/\(target.lastPathComponent)",
            taskID: pending.taskID
        )
        installedIdentity = nil
        return pending.version
    }

    public func cancelOutput(fileID: String) {
        pendingOutputs.removeValue(forKey: fileID)
    }

    public func persistedVersion(fileID: String) async throws -> HostFileVersion? {
        try await store.fileVersion(fileID: fileID)
    }

    private static func ensurePrivateDirectory(_ url: URL) throws {
        let manager = FileManager.default
        if !manager.fileExists(atPath: url.path) {
            try manager.createDirectory(at: url, withIntermediateDirectories: false,
                                        attributes: [.posixPermissions: 0o700])
        }
        var status = stat()
        guard lstat(url.path, &status) == 0,
              status.st_mode & S_IFMT == S_IFDIR,
              status.st_uid == getuid(), status.st_mode & 0o077 == 0,
              url.resolvingSymlinksInPath().path == url.path else {
            throw FileBoundaryError.invalidPath
        }
    }

    private static func copy(_ source: RuntimeOpenedFile, to descriptor: Int32,
                             chunkBytes: Int) throws {
        var offset: Int64 = 0
        while offset < source.byteCount {
            let bytes = try source.readChunk(offset: offset, maximumBytes: chunkBytes)
            guard !bytes.isEmpty else { throw FileBoundaryError.contentMismatch }
            try writeAll(bytes, descriptor: descriptor)
            offset += Int64(bytes.count)
        }
    }

    private static func writeAll(_ data: Data, descriptor: Int32) throws {
        var completed = 0
        while completed < data.count {
            let count = data.withUnsafeBytes { bytes in
                Darwin.write(descriptor, bytes.baseAddress! + completed, data.count - completed)
            }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else { throw FileBoundaryError.system(errno) }
            completed += count
        }
    }

    fileprivate static func removeIfIdentityMatches(path: String, identity: RuntimeFileIdentity) {
        var status = stat()
        guard lstat(path, &status) == 0,
              status.st_mode & S_IFMT == S_IFREG,
              RuntimeFileIdentity(device: UInt64(status.st_dev), inode: UInt64(status.st_ino)) == identity else {
            return
        }
        _ = Darwin.unlink(path)
    }

    private static func identity(of descriptor: Int32) throws -> RuntimeFileIdentity {
        var status = stat()
        guard fstat(descriptor, &status) == 0 else { throw FileBoundaryError.system(errno) }
        return RuntimeFileIdentity(device: UInt64(status.st_dev), inode: UInt64(status.st_ino))
    }

    private static func validate(_ data: Data, mediaType: HostFileMediaType) throws {
        guard !data.isEmpty, !data.contains(0), String(data: data, encoding: .utf8) != nil else {
            throw FileBoundaryError.invalidText
        }
        switch mediaType {
        case .csv: try validateCSV(data)
        case .markdown: try validateMarkdown(data)
        }
    }

    private static func validateCSV(_ data: Data) throws {
        guard let text = String(data: data, encoding: .utf8) else { throw FileBoundaryError.invalidText }
        var rows: [[String]] = [[]]
        var field = ""
        var quoted = false
        var closedQuote = false
        var index = text.startIndex
        while index < text.endIndex {
            let character = text[index]
            if quoted {
                if character == "\"" {
                    let next = text.index(after: index)
                    if next < text.endIndex, text[next] == "\"" {
                        field.append("\"")
                        index = next
                    } else { quoted = false; closedQuote = true }
                } else { field.append(character) }
            } else if character == "\"", field.isEmpty, !closedQuote {
                quoted = true
            } else if character == "," {
                rows[rows.count - 1].append(field); field = ""; closedQuote = false
            } else if character == "\n" {
                rows[rows.count - 1].append(field); field = ""; closedQuote = false; rows.append([])
            } else if character != "\r" {
                guard !closedQuote, character != "\"" else { throw FileBoundaryError.invalidText }
                field.append(character)
            }
            index = text.index(after: index)
        }
        guard !quoted else { throw FileBoundaryError.invalidText }
        if !field.isEmpty || !rows[rows.count - 1].isEmpty { rows[rows.count - 1].append(field) }
        if rows.last?.isEmpty == true { rows.removeLast() }
        guard let header = rows.first, header.count >= 2,
              header.allSatisfy({ !$0.trimmingCharacters(in: .whitespaces).isEmpty }),
              Set(header).count == header.count, rows.count >= 2,
              rows.dropFirst().allSatisfy({ $0.count == header.count }) else {
            throw FileBoundaryError.invalidText
        }
    }

    private static func validateMarkdown(_ data: Data) throws {
        guard let text = String(data: data, encoding: .utf8) else { throw FileBoundaryError.invalidText }
        let lines = text.split(whereSeparator: { $0.isNewline })
        let hasStructure = lines.contains { line in
            let value = line.trimmingCharacters(in: .whitespaces)
            return value.hasPrefix("# ") || value.hasPrefix("## ") || value.hasPrefix("- ") ||
                value.hasPrefix("* ") || value.hasPrefix("> ") || value.hasPrefix("```") ||
                (value.hasPrefix("|") && value.hasSuffix("|"))
        }
        guard hasStructure else { throw FileBoundaryError.invalidText }
    }

    private static func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(value)
    }
}

extension WorkspaceStore {
    fileprivate func recordInputVersion(_ version: HostFileVersion, objectKey: String) throws {
        let role = "input"
        guard !database.readOnly,
              !objectKey.hasPrefix("/"), !objectKey.contains("..") else {
            throw FileBoundaryError.invalidPath
        }
        let eventObject: [String: Any] = [
            "fileId": version.fileID, "role": role, "sha256": version.sha256,
            "size": version.size, "mediaType": version.mediaType.rawValue,
        ]
        let event = try JSONSerialization.data(withJSONObject: eventObject, options: [.sortedKeys])
        let payload = String(decoding: event, as: UTF8.self)
        try database.transaction {
            try database.query(
                "INSERT INTO file_versions VALUES(?,?,?,?,?,'complete','live',NULL)",
                [version.fileID, "\(role):\(version.mediaType.rawValue)", objectKey,
                 version.sha256, String(version.size)]
            )
            try appendEvent(type: "file.\(role).committed", payload: payload)
        }
    }

    fileprivate func requireArtifactBegin(fileID: String, taskID: String) throws {
        guard !database.readOnly,
              try writableTaskExists(taskID),
              try database.query("SELECT 1 FROM file_versions WHERE id=?", [fileID]).isEmpty else {
            throw FileBoundaryError.unavailable
        }
    }

    fileprivate func recordArtifactVersion(_ version: HostFileVersion, role: String,
                                            objectKey: String, taskID: String) throws {
        guard role == "artifact", !database.readOnly,
              !objectKey.hasPrefix("/"), !objectKey.contains("..") else {
            throw FileBoundaryError.invalidPath
        }
        let provenanceID = "prov_" + UUID().uuidString.lowercased()
        let manifestData = try JSONSerialization.data(
            withJSONObject: ["taskId": taskID], options: [.sortedKeys]
        )
        let manifest = String(decoding: manifestData, as: UTF8.self)
        let ruleDigest = SHA256.hash(data: Data("havenbox-file-boundary-v1".utf8))
            .map { String(format: "%02x", $0) }.joined()
        let eventData = try JSONSerialization.data(withJSONObject: [
            "fileId": version.fileID, "role": role, "sha256": version.sha256,
            "size": version.size, "mediaType": version.mediaType.rawValue, "taskId": taskID,
        ], options: [.sortedKeys])
        try database.transaction {
            guard try writableTaskExists(taskID) else {
                throw FileBoundaryError.unavailable
            }
            try database.query(
                "INSERT INTO provenance VALUES(?,?,?,?,?,unixepoch())",
                [provenanceID, "host-file-boundary-v1", "runtime-file-protocol-v1", ruleDigest, manifest]
            )
            try database.query(
                "INSERT INTO file_versions VALUES(?,?,?,?,?,'complete','live',?)",
                [version.fileID, "\(role):\(version.mediaType.rawValue)", objectKey,
                 version.sha256, String(version.size), provenanceID]
            )
            try database.query("INSERT INTO task_files VALUES(?,?,?)", [taskID, version.fileID, role])
            try appendEvent(type: "file.artifact.committed",
                            payload: String(decoding: eventData, as: UTF8.self))
        }
    }

    private func writableTaskExists(_ taskID: String) throws -> Bool {
        !(try database.query(
            """
            SELECT 1 FROM tasks t
            JOIN active_slot a ON a.turn_id=t.turn_id AND a.state='active'
            JOIN approvals p ON p.task_id=t.id AND p.kind='execution'
              AND p.revoked_at IS NULL AND p.expires_at>=unixepoch()
            WHERE t.id=? AND t.state IN ('approved','running') LIMIT 1
            """,
            [taskID]
        )).isEmpty
    }

    public func fileVersion(fileID: String) throws -> HostFileVersion? {
        _ = try FileVersionID(fileID)
        guard let row = try database.query(
            """
            SELECT kind,sha256,size FROM file_versions
            WHERE id=? AND completeness='complete' AND storage_state='live'
              AND (kind LIKE 'input:%' OR (kind LIKE 'artifact:%' AND provenance_id IS NOT NULL
                AND EXISTS(SELECT 1 FROM task_files
                           WHERE file_version_id=file_versions.id AND role='artifact')))
            """,
            [fileID]
        ).first, let kind = row[0], let digest = row[1], let sizeText = row[2],
              let size = UInt64(sizeText), let separator = kind.firstIndex(of: ":"),
              let mediaType = HostFileMediaType(rawValue: String(kind[kind.index(after: separator)...])) else {
            return nil
        }
        return try HostFileVersion(fileID: fileID, sha256: digest, size: size, mediaType: mediaType)
    }
}
