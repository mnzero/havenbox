// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Havenbox",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "HavenboxCore", targets: ["HavenboxCore"]),
        .executable(name: "Havenbox", targets: ["HavenboxApp"]),
        .executable(name: "havenbox-helper", targets: ["HavenboxHelper"]),
    ],
    targets: [
        .target(name: "CSQLite", exclude: ["NOTICE.md"], publicHeadersPath: "include",
                cSettings: [.define("SQLITE_THREADSAFE", to: "1"), .define("SQLITE_OMIT_LOAD_EXTENSION")]),
        .target(name: "HavenboxCore", dependencies: ["CSQLite"]),
        .executableTarget(name: "HavenboxApp", dependencies: ["HavenboxCore"]),
        .executableTarget(name: "HavenboxHelper", dependencies: ["HavenboxCore"]),
        .executableTarget(name: "HavenboxCoreTests", dependencies: ["HavenboxCore"], path: "tests/HavenboxCoreTests"),
    ]
)
