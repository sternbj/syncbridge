import Foundation

/// Starts SyncBridge at login through a per-user LaunchAgent.
/// This works for unsigned builds, unlike SMAppService.
enum LoginItem {
    static let label = "io.github.sternbj.syncbridge"
    /// Passed when SyncBridge starts at login, so it stays in the menu bar without a window.
    static let backgroundFlag = "--background"
    static var launchedInBackground: Bool { CommandLine.arguments.contains(backgroundFlag) }

    static var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    static var isEnabled: Bool {
        FileManager.default.fileExists(atPath: plistURL.path)
    }

    static func setEnabled(_ enabled: Bool) throws {
        if !enabled {
            try? FileManager.default.removeItem(at: plistURL)
            return
        }
        let plist: [String: Any] = [
            "Label": label,
            "ProgramArguments": ["/usr/bin/open", "-g", "-a", Bundle.main.bundlePath, "--args", backgroundFlag],
            "RunAtLoad": true,
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try FileManager.default.createDirectory(
            at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try data.write(to: plistURL)
    }
}
