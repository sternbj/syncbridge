import AppKit
import SwiftUI

/// SyncBridge lives in the menu bar. While one of its windows is open it acts
/// as a normal app (Dock icon and its own menu bar with Settings, Sync, Window),
/// and returns to the menu bar when the last window closes.
@MainActor
final class WindowActivation {
    static let shared = WindowActivation()
    private var observers: [NSObjectProtocol] = []

    func start() {
        guard observers.isEmpty else { return }
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main) { note in
            guard let window = note.object as? NSWindow else { return }
            Task { @MainActor in
                if Self.isAppWindow(window), NSApp.activationPolicy() != .regular {
                    NSApp.setActivationPolicy(.regular)
                    Self.takeMenuBar(window)
                }
            }
        })
        observers.append(center.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { note in
            guard let closing = note.object as? NSWindow else { return }
            Task { @MainActor in
                guard Self.isAppWindow(closing) else { return }
                // The closing window may still be listed as visible right now.
                try? await Task.sleep(nanoseconds: 300_000_000)
                if !NSApp.windows.contains(where: { $0 !== closing && $0.isVisible && Self.isAppWindow($0) }) {
                    NSApp.setActivationPolicy(.accessory)
                }
            }
        })
    }

    /// An app that turns from menu bar only into a regular app while already active
    /// does not get the menu bar: it stays frozen until the app is activated anew.
    /// Handing focus to the Dock for a moment and taking it back fixes that.
    private static func takeMenuBar(_ window: NSWindow) {
        NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock").first?.activate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
        }
    }

    /// Titled windows only; the menu bar extra's own panels are not.
    static func isAppWindow(_ window: NSWindow) -> Bool {
        window.styleMask.contains(.titled)
    }
}

/// Quick sync options, shown in the menu bar menu and in the app's Sync menu.
struct SyncOptionsMenu: View {
    @ObservedObject var model: AppModel
    @AppStorage(AppModel.realtimeKey) private var realtime = false

    var body: some View {
        Toggle("Sync When Files Change", isOn: $realtime)
            .onChange(of: realtime) { _ in model.updateWatcher() }
        Picker("Sync Every", selection: Binding(
            get: { model.intervalMinutes },
            set: { minutes in Task { await model.setInterval(minutes) } }
        )) {
            ForEach(AppModel.intervalChoices, id: \.minutes) { Text($0.label).tag($0.minutes) }
        }
    }
}

/// Window and sync commands for the app's menu bar.
struct SyncCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandGroup(replacing: .appSettings) {
            Button("Settings…") { model.show("settings") }.keyboardShortcut(",")
        }
        CommandGroup(replacing: .help) { FeedbackButtons() }
        CommandMenu("Sync") {
            Button("Sync Now") { Task { await model.syncNow() } }.keyboardShortcut("s")
            Button("Compare & Preview…") { model.show("preview") }
            Button("Conflicts…") { model.show("conflicts") }
            Button("Pending Deletions…") { model.show("deletions") }
            Divider()
            SyncOptionsMenu(model: model)
        }
    }
}

/// Opening SyncBridge again (Finder, Spotlight, Launchpad, Dock) while it is
/// already running shows the Settings window, so the app is reachable even
/// when macOS hides its menu bar icon (notch, or "Allow in the Menu Bar" off).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static var onReopen: (() -> Void)?

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { Self.onReopen?() }
        return true
    }
}
