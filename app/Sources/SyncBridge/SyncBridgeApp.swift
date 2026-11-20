import SwiftUI

@main
struct SyncBridgeApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        MenuBarExtra {
            MenuContent()
                .environmentObject(model)
        } label: {
            MenuBarLabel(model: model)
        }
        .menuBarExtraStyle(.menu)

        Window("Conflicts", id: "conflicts") {
            ConflictsView()
                .environmentObject(model)
        }
        .defaultSize(width: 900, height: 560)
        .windowResizability(.contentMinSize)

        Window("Pending Deletions", id: "deletions") {
            DeletionsView()
                .environmentObject(model)
        }
        .defaultSize(width: 760, height: 520)
        .windowResizability(.contentMinSize)

        Window("Compare & Preview", id: "preview") {
            PreviewView()
                .environmentObject(model)
        }
        .defaultSize(width: 980, height: 600)
        .windowResizability(.contentMinSize)

        Window("SyncBridge Settings", id: "settings") {
            SettingsView()
                .environmentObject(model)
        }
        .defaultSize(width: 900, height: 620)
        .commands { SyncCommands(model: model) }
    }
}

struct MenuContent: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Text(model.isSyncing ? "Syncing…" : model.lastSyncText)
        if let result = model.status?.lastResult, !result.isEmpty {
            Text(result)
        }
        Divider()
        Button("Sync Now") { Task { await model.syncNow() } }
            .disabled(model.isSyncing)
            .keyboardShortcut("s")
        Button("Compare & Preview…") { show("preview") }
        Button("Conflicts (\(model.status?.conflicts ?? 0))…") { show("conflicts") }
        Button("Pending Deletions (\(model.status?.pendingDeletions ?? 0))…") { show("deletions") }
        Divider()
        SyncOptionsMenu(model: model)
        Divider()
        Button("Settings…") { show("settings") }
            .keyboardShortcut(",")
        Button("Open Sync Log") { model.open(model.status?.logFile) }
        Button("Open Last Summary") { model.open(model.status?.summaryFile) }
        Menu("Help") { FeedbackButtons() }
        Divider()
        Button("Quit SyncBridge") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func show(_ id: String) { model.show(id) }
}

/// The menu bar icon. It is always on screen, so it hands SwiftUI's window
/// opener to the model for notification clicks.
struct MenuBarLabel: View {
    @ObservedObject var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Image(systemName: model.isSyncing ? "arrow.triangle.2.circlepath" : "arrow.triangle.2.circlepath.icloud")
            .onAppear { model.windowOpener = { openWindow(id: $0) } }
    }
}
