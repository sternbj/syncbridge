// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "SyncBridge",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "SyncBridge", path: "Sources/SyncBridge"),
        .testTarget(name: "SyncBridgeTests", dependencies: ["SyncBridge"], path: "Tests/SyncBridgeTests"),
    ]
)
