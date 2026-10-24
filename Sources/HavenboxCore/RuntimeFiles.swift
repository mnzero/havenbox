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
    }

    public let path: String
    public let identity: RuntimeFileIdentity
    private let descriptor: Int32
    private let metadata: Metadata

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

    private init(path: String, descriptor: Int32, metadata: Metadata) {
        self.path = path
        self.descriptor = descriptor
        self.metadata = metadata
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
            guard fstat(next, &status) == 0,
                  status.st_mode & S_IFMT == S_IFDIR,
                  status.st_mode & 0o022 == 0 else {
                Darwin.close(next)
                throw RuntimeError.assetRejected("writable or invalid file parent")
            }
            Darwin.close(directory)
            directory = next
        }
        let flags: Int32
        switch requirement {
        case .writableData: flags = O_RDWR | O_NOFOLLOW | O_CLOEXEC
        default: flags = O_RDONLY | O_NOFOLLOW | O_CLOEXEC
        }
        let leaf = String(components.last!)
        let descriptor = leaf.withCString { Darwin.openat(directory, $0, flags) }
        guard descriptor >= 0 else { throw RuntimeError.assetRejected("file open rejected") }
        var owner: RuntimeOpenedFile?
        do {
            let before = try checkedMetadata(descriptor)
            try validate(before, requirement: requirement)
            let opened = RuntimeOpenedFile(path: path, descriptor: descriptor, metadata: before)
            owner = opened
            switch requirement {
            case let .immutable(expected, _):
                guard try opened.sha256() == expected else {
                    throw RuntimeError.assetRejected("asset digest mismatch")
                }
            case let .configuration(maximumBytes):
                _ = try opened.readAll(maximumBytes: maximumBytes)
            case .writableData:
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
        }
    }
}
