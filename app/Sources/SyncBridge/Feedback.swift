import AppKit
import SwiftUI

/// Bug reports and ideas go to GitHub issues, prefilled with the app and macOS version.
enum Feedback {
    static let repo = "https://github.com/sternbj/syncbridge"

    static var versionText: String {
        let app = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
        let os = ProcessInfo.processInfo.operatingSystemVersion
        return "SyncBridge \(app), macOS \(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"
    }

    static func open(template: String) {
        var components = URLComponents(string: "\(repo)/issues/new")!
        components.queryItems = [
            URLQueryItem(name: "template", value: template),
            URLQueryItem(name: "version", value: versionText),
        ]
        NSWorkspace.shared.open(components.url!)
    }

    static func reportBug() { open(template: "bug_report.yml") }
    static func suggestIdea() { open(template: "feature_request.yml") }
    static func openProject() { NSWorkspace.shared.open(URL(string: repo)!) }
    static func star() {
        StarPrompt.finish()
        openProject()
    }
}

/// Asks once in a while for a star on GitHub, only after SyncBridge has proven itself:
/// at least a week of use and 20 good syncs. "Later" waits 30 days; a star or
/// "Don't Ask Again" ends it for good.
enum StarPrompt {
    private static let firstUseKey = "starFirstUse"
    private static let syncsKey = "starGoodSyncs"
    private static let nextAskKey = "starNextAsk"
    private static let doneKey = "starDone"
    static let minimumDays = 7
    static let minimumSyncs = 20

    static func syncSucceeded() {
        let defaults = UserDefaults.standard
        if defaults.object(forKey: firstUseKey) == nil { defaults.set(Date(), forKey: firstUseKey) }
        defaults.set(defaults.integer(forKey: syncsKey) + 1, forKey: syncsKey)
    }

    static var isDue: Bool {
        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: doneKey),
              let firstUse = defaults.object(forKey: firstUseKey) as? Date,
              Date().timeIntervalSince(firstUse) >= Double(minimumDays) * 86_400,
              defaults.integer(forKey: syncsKey) >= minimumSyncs else { return false }
        let nextAsk = defaults.object(forKey: nextAskKey) as? Date ?? .distantPast
        return Date() >= nextAsk
    }

    static func later() {
        UserDefaults.standard.set(Date().addingTimeInterval(30 * 86_400), forKey: nextAskKey)
    }

    static func finish() {
        UserDefaults.standard.set(true, forKey: doneKey)
    }
}

/// The alert that asks for a star; attach to a window the user opened.
struct StarPromptModifier: ViewModifier {
    @State private var showing = false

    func body(content: Content) -> some View {
        content
            .task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                showing = StarPrompt.isDue
            }
            .alert("Enjoying SyncBridge?", isPresented: $showing) {
                Button("Star on GitHub") { Feedback.star() }
                Button("Later") { StarPrompt.later() }
                Button("Don't Ask Again", role: .cancel) { StarPrompt.finish() }
            } message: {
                Text("""
                SyncBridge is free and open source. A star on GitHub helps other people find it, \
                and more users mean more testing and better support for everyone.

                Found a bug or have an idea? Settings › Help & Feedback.
                """)
            }
    }
}

/// The same three entries in the menu bar menu and the app's Help menu.
struct FeedbackButtons: View {
    var body: some View {
        Button("Report a Bug…") { Feedback.reportBug() }
        Button("Suggest an Idea…") { Feedback.suggestIdea() }
        Button("Star SyncBridge on GitHub ★") { Feedback.star() }
    }
}

/// Settings › Help & Feedback.
struct FeedbackSettingsView: View {
    var body: some View {
        Form {
            Section {
                feedbackRow("Report a Bug…", icon: "ladybug", action: Feedback.reportBug,
                            text: "Something doesn't work or looks wrong. The form already contains your app and macOS version.")
                feedbackRow("Suggest an Idea…", icon: "lightbulb", action: Feedback.suggestIdea,
                            text: "A feature you are missing or something that could be easier.")
            } header: {
                Text("Tell us")
            } footer: {
                Text("Both open a short form on GitHub. You need a free GitHub account. Never paste file contents or personal data.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Support SyncBridge") {
                feedbackRow("Star on GitHub ★", icon: "star", action: Feedback.star,
                            text: "Free and quick. Stars help other people find SyncBridge, which brings more testing and better support.")
                feedbackRow("Contribute Code…", icon: "chevron.left.forwardslash.chevron.right",
                            action: { NSWorkspace.shared.open(URL(string: Feedback.repo + "/blob/main/CONTRIBUTING.md")!) },
                            text: "SyncBridge is open source. Pull requests are welcome; the tests run automatically.")
            }
            Section {
                LabeledContent("Version", value: Feedback.versionText).textSelection(.enabled)
            }
        }
    }

    private func feedbackRow(_ title: String, icon: String, action: @escaping () -> Void, text: String) -> some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Label(title.replacingOccurrences(of: "…", with: ""), systemImage: icon)
                Text(text).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button(title.hasSuffix("…") ? "Open…" : "Open") { action() }
        }
    }
}
