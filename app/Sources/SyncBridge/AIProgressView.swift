import SwiftUI

/// "Asking AI… 3 of 12" with a progress bar, the file being looked at and a Stop button.
/// Shown while "Ask AI for All" or "Try on Open Conflicts" runs.
struct AIProgressView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        if let progress = model.aiProgress {
            HStack(spacing: 8) {
                ProgressView(value: Double(progress.done), total: Double(max(progress.total, 1)))
                    .frame(width: 80)
                VStack(alignment: .leading, spacing: 0) {
                    Text(model.aiStopRequested ? "Stopping after this file…" : "Asking AI… \(progress.done) of \(progress.total)")
                        .monospacedDigit()
                    if let file = currentFile {
                        Text(file).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    }
                }
                Button("Stop") { model.stopAI() }
                    .disabled(model.aiStopRequested)
                    .help("Stops after the answer the AI is working on. Suggestions so far are kept.")
            }
        }
    }

    private var currentFile: String? {
        model.conflicts.first { $0.id == model.aiAsking }.map { ($0.file as NSString).lastPathComponent }
    }
}

/// A small marker for one conflict: a spinner while the AI looks at it, a sparkle once it answered.
struct AIStatusMark: View {
    @EnvironmentObject var model: AppModel
    let conflict: Conflict

    var body: some View {
        if model.aiAsking == conflict.id {
            ProgressView().controlSize(.small).help("The AI is looking at this file.")
        } else if let suggestion = model.aiSuggestions[conflict.id] {
            Image(systemName: "sparkles")
                .foregroundStyle(suggestion.keep == "ask" ? Color.secondary : Color.accentColor)
                .help("AI suggests: \(suggestion.title)")
        }
    }
}
