import SwiftUI

/// A conflict where the user chose differently than the AI suggested.
struct AICorrection: Identifiable {
    let id = UUID()
    let file: String
    let suggested: AIDecision
    let userChoice: String

    var userTitle: String {
        switch userChoice {
        case "local": return "Keep Local"
        case "onedrive": return "Keep OneDrive"
        case "merged": return "Merge"
        default: return userChoice
        }
    }
}

/// Asks what the AI got wrong and turns the answer into a new instruction,
/// which the user can edit before it is added.
struct AICorrectionView: View {
    let correction: AICorrection
    let onDone: () -> Void
    @ObservedObject private var trust = AITrust.shared
    @State private var feedback = ""
    @State private var proposal = ""
    @State private var working = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Help the AI learn").font(.title3.bold())
            Text((correction.file as NSString).lastPathComponent).foregroundStyle(.secondary)
            Text("The AI suggested **\(correction.suggested.title)**, you chose **\(correction.userTitle)**.")
            if !correction.suggested.reason.isEmpty {
                Text("Its reason: \(correction.suggested.reason)").font(.callout).foregroundStyle(.secondary)
            }
            Text("What did the AI get wrong?").bold()
            TextEditor(text: $feedback)
                .frame(minHeight: 70)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
            HStack {
                Button("Suggest an Instruction") { Task { await suggest() } }
                    .disabled(feedback.trimmingCharacters(in: .whitespaces).isEmpty || working
                              || !AIAssistant.provider.writesText)
                if !AIAssistant.provider.writesText {
                    Text("Jev can't write text. Type the instruction yourself below.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if working { ProgressView().controlSize(.small) }
                if let error { Text(error).font(.caption).foregroundStyle(.red) }
            }
            if !proposal.isEmpty || working || !AIAssistant.provider.writesText {
                Text("New instruction (you can edit it)").bold()
                TextField("Instruction", text: $proposal, axis: .vertical)
                    .lineLimit(2...4)
            }
            Text("""
            Adding an instruction starts the agreement count again, so automatic decisions only \
            come back after a full run of matching decisions.
            """)
            .font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Skip", action: onDone).keyboardShortcut(.cancelAction)
                Button("Add to Instructions") {
                    trust.addInstruction(proposal)
                    onDone()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(proposal.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding()
        .frame(width: 520)
    }

    private func suggest() async {
        working = true
        error = nil
        defer { working = false }
        do {
            proposal = try await AIAssistant().learn(
                file: correction.file, suggested: correction.suggested,
                userChoice: correction.userTitle, feedback: feedback
            )
        } catch {
            self.error = error.localizedDescription
        }
    }
}
