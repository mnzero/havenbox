import Foundation

/// Monotonic, one-shot control lease. Neither reconnect nor a late heartbeat revives it.
public struct RuntimeWatchdog {
    public enum State: Equatable { case awaitingHello, active, terminated(String) }
    public private(set) var state: State = .awaitingHello
    public let instance: UUID
    public let epoch: UInt64
    private var deadline: UInt64
    private var lastTick: UInt64
    private var sequence: UInt64 = 0
    private let ttl: UInt64

    public init(instance: UUID, epoch: UInt64, now: UInt64, ttl: UInt64) throws {
        guard epoch > 0, ttl > 0, ttl <= 30_000_000_000, now <= UInt64.max - ttl else {
            throw RuntimeError.invalidConfiguration
        }
        self.instance = instance; self.epoch = epoch; self.ttl = ttl
        self.deadline = now + ttl; self.lastTick = now
    }

    @discardableResult public mutating func tick(now: UInt64) -> Bool {
        if case .terminated = state { return false }
        guard now >= lastTick, now < deadline else {
            state = .terminated("lease expired or clock regressed"); return false
        }
        lastTick = now
        return true
    }

    public mutating func hello(instance: UUID, epoch: UInt64, now: UInt64) throws {
        guard tick(now: now), state == .awaitingHello,
              instance == self.instance, epoch == self.epoch else {
            disconnect(reason: "invalid handshake"); throw RuntimeError.invalidControl
        }
        state = .active
    }

    public mutating func heartbeat(sequence: UInt64, now: UInt64) throws {
        guard tick(now: now), state == .active, sequence > self.sequence,
              now <= UInt64.max - ttl else {
            disconnect(reason: "invalid heartbeat"); throw RuntimeError.invalidControl
        }
        self.sequence = sequence; deadline = now + ttl
    }

    public mutating func disconnect(reason: String = "control disconnected") {
        if case .terminated = state { return }
        state = .terminated(reason)
    }
}

public enum RuntimeError: Error { case invalidConfiguration, invalidControl, assetRejected(String), unavailable(String) }
