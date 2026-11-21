import SwiftUI

/// Settings › AI: the user's instructions, a dry run on the open conflicts,
/// and automatic decisions once the AI has earned them.
struct AIInstructionsView: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject private var trust = AITrust.shared
    let provider: AIProvider
    @State private var tryError: String?
    @State private var confirming = false

    var body: some View {
        Section {
            TextEditor(text: $trust.instructions)
                .font(.body)
                .frame(minHeight: 90)
                .onChange(of: trust.instructions) { _ in model.refreshSuggestions() }
        } header: {
            Text("Your instructions")
        } footer: {
            VStack(alignment: .leading, spacing: 6) {
                Text("""
                Tell the AI how to judge the content, one rule per line. Fixed rules (never delete in a \
                folder, one person's changes win) work more reliably in Settings › Conflicts. When you \
                overrule the AI, SyncBridge asks why and suggests a new instruction.
                """)
                Text("Examples:")
                ForEach(AITrust.examples, id: \.self) { Text("• \($0)") }
            }
            .font(.caption).foregroundStyle(.secondary)
            .textSelection(.enabled)
        }

        Section {
            HStack {
                if model.aiWorking {
                    AIProgressView()
                } else {
                    Button("Try on Open Conflicts") {
                        Task {
                            await model.loadConflicts()
                            tryError = await model.suggestAll()
                        }
                    }
                }
                Spacer()
                OpenConflictsButton()
            }
            if let tryError { Text("AI help failed: \(tryError)").font(.caption).foregroundStyle(.red) }
            ForEach(model.conflicts.filter { model.aiSuggestions[$0.id] != nil }) { conflict in
                if let suggestion = model.aiSuggestions[conflict.id] {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text((conflict.file as NSString).lastPathComponent).lineLimit(1)
                            Spacer()
                            Text(suggestion.title).bold()
                                .foregroundStyle(suggestion.keep == "ask" ? Color.secondary : Color.accentColor)
                        }
                        Text(suggestion.reason).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        } header: {
            Text("Test your instructions")
        } footer: {
            Text("""
            Shows what the AI would do with your open conflicts. Nothing is changed. Answers are \
            kept until a file or your instructions change, so after editing the instructions, try \
            again until the answers match what you would do; then decide in \
            the Conflicts window, where "Use AI Suggestions" fills in its choices for you to check.
            """ + (provider.isCloud ? " The text of each file is sent to \(provider.serviceName)." : ""))
            .font(.caption).foregroundStyle(.secondary)
        }

        Section {
            Text(trust.summary)
            LabeledContent("Agreement in total", value: AITrust.rate(trust.records))
            LabeledContent("Last \(AITrust.windowDays) days, current instructions", value: AITrust.rate(trust.window))
            Toggle("Let the AI decide automatically", isOn: Binding(
                get: { trust.autoDecide },
                set: { on in if on { confirming = true } else { trust.autoDecide = false } }
            ))
            .disabled(!trust.isEligible && !trust.autoDecide)
            .alert("Let the AI decide automatically?", isPresented: $confirming) {
                Button("Cancel", role: .cancel) {}
                Button("Turn On") { trust.autoDecide = true }
            } message: {
                Text(trust.confirmationText)
            }
        } header: {
            Text("Automatic decisions")
        } footer: {
            Text("""
            Unlocks only when the AI agreed with every one of at least \(AITrust.minimumDecisions) of \
            your decisions in the last \(AITrust.windowDays) days, with the current instructions. One \
            different choice or changed instructions switch it off again. When on, the AI resolves \
            conflicts after each sync where it is sure and both versions exist; deletions, name \
            conflicts and merges always wait for you. Overwritten versions are kept as set under \
            Deletions & Versions, and a notification lists what the AI decided.
            """ + (provider.isCloud ? " File text is then sent to \(provider.serviceName) during syncs." : ""))
            .font(.caption).foregroundStyle(.secondary)
        }
    }
}
