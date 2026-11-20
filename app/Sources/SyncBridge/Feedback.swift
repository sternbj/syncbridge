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
}

/// The same three entries in the menu bar menu and the app's Help menu.
struct FeedbackButtons: View {
    var body: some View {
        Button("Report a Bug…") { Feedback.reportBug() }
        Button("Suggest an Idea…") { Feedback.suggestIdea() }
        Button("SyncBridge on GitHub") { Feedback.openProject() }
    }
}
