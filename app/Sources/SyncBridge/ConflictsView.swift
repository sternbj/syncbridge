import SwiftUI

/// Lists open conflicts. Decisions are collected first (one by one or for all
/// at once) and then applied in a single engine call.
struct ConflictsView: View {
    @EnvironmentObject var model: AppModel
    @State private var selection: Conflict.ID?
    @State private var decisions: [Conflict.ID: String] = [:]
    @State private var result: String?
    @State private var mergedTexts: [Conflict.ID: String] = [:]
    @State private var aiAnswers: [Conflict.ID: String] = [:]
    @State private var aiBusy: Conflict.ID?
    @State private var proposal: MergeProposal?
    @State private var corrections: [AICorrection] = []
    @AppStorage(AppModel.realtimeKey) private var realtime = false
    @AppStorage(AIAssistant.providerKey) private var aiProvider = AIProvider.off.rawValue

    private var selected: Conflict? { model.conflicts.first { $0.id == selection } }

    var body: some View {
        VStack(spacing: 0) {
            bulkBar
            Divider()
            HSplitView {
                list.frame(minWidth: 320, idealWidth: 380)
                detail.frame(minWidth: 380)
            }
            Divider()
            applyBar
        }
        .task { await model.loadConflicts() }
        .sheet(item: $proposal) { proposal in
            MergeReviewView(proposal: proposal) { text in
                mergedTexts[proposal.id] = text
                decisions[proposal.id] = "merged"
                self.proposal = nil
            } onCancel: {
                self.proposal = nil
            }
        }
        .sheet(item: Binding(get: { corrections.first }, set: { if $0 == nil, !corrections.isEmpty { corrections.removeFirst() } })) { correction in
            AICorrectionView(correction: correction) { corrections.removeFirst() }
        }
        .alert("Conflicts", isPresented: Binding(get: { result != nil }, set: { if !$0 { result = nil } })) {
            Button("OK") { result = nil }
        } message: {
            Text(result ?? "")
        }
    }

    // MARK: Bulk actions

    private var aiOn: Bool { AIProvider(rawValue: aiProvider) ?? .off != .off }

    private var bulkBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("\(model.conflicts.count) open conflict(s)").font(.headline).fixedSize()
                if !realtime {
                    Button("Fewer conflicts: sync when files change") {
                        model.settingsTab = "general"
                        model.show("settings")
                    }
                    .buttonStyle(.link)
                    .help("Opens Settings › Schedule & General, where you turn on syncing when files change and choose how often to sync.")
                }
                Spacer()
                AIProgressView()
            }
            HStack {
                Text("For all:").foregroundStyle(.secondary)
                Button("Keep Suggested") {
                    decideAll { $0.analysis?.confidence == "low" ? nil : $0.analysis?.keepSide }
                }
                .help("Uses the suggestion wherever it is clear (high or medium confidence). Conflicts that need a merge stay undecided.")
                if aiOn {
                    Button { Task { if let error = await model.suggestAll() { result = "AI help failed: \(error)" } } } label: {
                        Label("Ask AI for All", systemImage: "sparkles")
                    }
                    .disabled(model.aiWorking)
                    .help("Asks the AI about every conflict where both versions exist, a few at a time. Nothing is changed. Answers are kept, and a notification tells you when it is done.")
                    Button { decideAll { conflict in model.aiSuggestions[conflict.id].flatMap { $0.keep == "ask" ? nil : $0.keep } } } label: {
                        Label("Use AI Suggestions", systemImage: "sparkles")
                    }
                    .disabled(model.aiSuggestions.isEmpty)
                    .help("Fills in the AI's choice wherever it answered. Check them before you apply.")
                }
                Button("Keep Newer") { decideAll { $0.newer ?? defaultForDeleted($0) } }
                    .help("Keeps the version changed last. If one side was deleted, the remaining file is kept. Same-time edits stay undecided.")
                Button("Accept Deletions") { decideAll { $0.deletedSide } }
                    .disabled(!model.conflicts.contains { $0.deletedSide != nil })
                    .help("Where a file was deleted on one side and edited on the other, deletes it on the other side too. The removed file is kept as set under Deletions & Versions.")
                Button("Keep Local") { decideAll { $0.local != nil ? "local" : nil } }
                Button("Keep OneDrive") { decideAll { $0.onedrive != nil ? "onedrive" : nil } }
                Button("Clear") { decisions.removeAll() }
                    .disabled(decisions.isEmpty)
            }
            .fixedSize()
        }
        .padding(10)
    }

    /// For a delete-vs-edit conflict, "newer" keeps the file that still exists.
    private func defaultForDeleted(_ conflict: Conflict) -> String? {
        conflict.deletedSide.map { $0 == "local" ? "onedrive" : "local" }
    }

    private func decideAll(_ rule: (Conflict) -> String?) {
        for conflict in model.conflicts {
            if let keep = rule(conflict) { decisions[conflict.id] = keep }
        }
    }

    // MARK: List and detail

    private var list: some View {
        List(model.conflicts, selection: $selection) { conflict in
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text((conflict.file as NSString).lastPathComponent).lineLimit(1)
                    Text((conflict.file as NSString).deletingLastPathComponent)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
                }
                Spacer()
                AIStatusMark(conflict: conflict)
                if let keep = decisions[conflict.id] {
                    Text(conflict.keepBadge(keep))
                        .font(.caption.bold())
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Capsule().fill(Color.accentColor.opacity(0.2)))
                }
            }
            .tag(conflict.id)
        }
    }

    @ViewBuilder private var detail: some View {
        if let conflict = selected {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text(conflict.file).font(.headline).textSelection(.enabled)
                    Text("\(conflict.pair) · \(conflict.reason)").foregroundStyle(.secondary)
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                        versionRow("Local", conflict.local, isNewer: conflict.newer == "local")
                        versionRow("OneDrive", conflict.onedrive, isNewer: conflict.newer == "onedrive")
                    }
                    if conflict.isNameConflict {
                        Text("""
                        The names look the same but are stored differently (capitalisation, or how a letter \
                        like "ä" is encoded). The version you keep also sets the name on both sides.
                        """)
                        .foregroundStyle(.secondary)
                    }
                    if let analysis = conflict.analysis { suggestionBox(conflict, analysis) }
                    HStack {
                        Button(conflict.keepTitle("local")) { decide(conflict, "local") }
                            .disabled(conflict.local == nil && conflict.onedrive == nil)
                        Button(conflict.keepTitle("onedrive")) { decide(conflict, "onedrive") }
                        if mergedTexts[conflict.id] != nil {
                            Button(conflict.keepTitle("merged")) { decide(conflict, "merged") }
                        }
                        Button("Skip") { decisions[conflict.id] = nil; selectNext(after: conflict) }
                    }
                    aiSection(conflict)
                    if conflict.isText, !conflict.diffPreview.isEmpty {
                        Text("Changes (Local vs. OneDrive)").font(.subheadline.bold())
                        Text(conflict.diffPreview)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                            .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.1)))
                    }
                }
                .padding()
            }
        } else {
            Text(model.conflicts.isEmpty ? "No open conflicts 🎉" : "Select a conflict")
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    // MARK: Suggestion and AI help

    private func suggestionBox(_ conflict: Conflict, _ analysis: ConflictAnalysis) -> some View {
        let title: String
        switch analysis.suggestion {
        case "local", "onedrive": title = "Suggested: \(conflict.keepTitle(analysis.suggestion ?? ""))"
        case "merge": title = "Suggested: Merge — both sides have their own changes"
        default: title = "No suggestion"
        }
        let color: Color = analysis.confidence == "high" ? .green : analysis.confidence == "medium" ? .orange : .secondary
        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: "lightbulb")
                Text(title).bold()
                if let confidence = analysis.confidence {
                    Text("\(confidence) confidence").font(.caption.bold()).foregroundStyle(color)
                }
                Spacer()
                if let side = analysis.keepSide {
                    Button("Use Suggestion") { decide(conflict, side) }
                }
            }
            Text(analysis.reason).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(color.opacity(0.08)))
    }

    @ViewBuilder private func aiSection(_ conflict: Conflict) -> some View {
        let provider = AIProvider(rawValue: aiProvider) ?? .off
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("AI help").font(.subheadline.bold())
                Spacer()
                if aiBusy == conflict.id || model.aiAsking.contains(conflict.id) {
                    ProgressView().controlSize(.small)
                    Text("AI is reading both versions…").font(.caption).foregroundStyle(.secondary)
                }
                if provider == .off {
                    Button("Set Up AI…") {
                        model.settingsTab = "ai"
                        model.show("settings")
                    }
                } else {
                    Button("Ask AI") { Task { await askAI(conflict) } }
                        .disabled(aiBusy != nil || model.aiWorking || !AppModel.aiCanDecide(conflict))
                        .help(AppModel.aiCanDecide(conflict) ? "" : "Deletions and name conflicts are always yours to decide.")
                    if conflict.analysis?.mergeable == true, provider.writesText {
                        Button("Propose Merge…") { Task { await proposeMerge(conflict) } }
                            .disabled(aiBusy != nil)
                    }
                }
            }
            if provider.isCloud {
                Text("Sends the text of both versions of this file to \(provider.serviceName).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if provider != .off, conflict.analysis?.mergeable != true, conflict.analysis?.suggestion == "merge" {
                Text("Word, Excel and PowerPoint files can't be merged automatically. Ask AI what each side changed, keep one version, then copy the other side's changes over by hand.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let suggestion = model.aiSuggestions[conflict.id] {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("AI suggests: \(suggestion.keep == "ask" ? "decide yourself" : conflict.keepTitle(suggestion.keep))")
                            .bold()
                        Spacer()
                        if suggestion.keep != "ask" {
                            Button("Use") { decide(conflict, suggestion.keep) }
                        }
                    }
                    if !suggestion.reason.isEmpty { Text(suggestion.reason) }
                    if !suggestion.changes.isEmpty {
                        Text(suggestion.changes).font(.callout).foregroundStyle(.secondary)
                    }
                }
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.accentColor.opacity(0.08)))
            }
            if let answer = aiAnswers[conflict.id] {
                Text(answer)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.accentColor.opacity(0.08)))
            }
        }
    }

    private func askAI(_ conflict: Conflict) async {
        aiBusy = conflict.id
        defer { aiBusy = nil }
        aiAnswers[conflict.id] = nil
        do {
            try await model.suggest(conflict)
        } catch {
            aiAnswers[conflict.id] = "AI help failed: \(error.localizedDescription)"
        }
    }

    private func proposeMerge(_ conflict: Conflict) async {
        aiBusy = conflict.id
        defer { aiBusy = nil }
        do {
            let texts = try await model.loadConflictTexts(conflict)
            if texts.truncated {
                aiAnswers[conflict.id] = "This file is too long to merge safely with AI. Keep one version instead."
                return
            }
            let merged = try await AIAssistant().merge(texts)
            proposal = MergeProposal(id: conflict.id, file: conflict.file, local: texts.localText ?? "",
                                     onedrive: texts.onedriveText ?? "", merged: merged)
        } catch {
            aiAnswers[conflict.id] = "AI merge failed: \(error.localizedDescription)"
        }
    }

    private func versionRow(_ label: String, _ version: FileVersion?, isNewer: Bool) -> some View {
        GridRow {
            Text(label).bold()
            if let version {
                Text(version.date.formatted(date: .abbreviated, time: .shortened))
                Text(ByteCountFormatter.string(fromByteCount: Int64(version.size), countStyle: .file))
                if isNewer {
                    Text("newer").font(.caption.bold()).foregroundStyle(.green)
                }
            } else {
                Text("deleted").foregroundStyle(.red)
            }
        }
    }

    private func decide(_ conflict: Conflict, _ keep: String) {
        decisions[conflict.id] = keep
        selectNext(after: conflict)
    }

    private func selectNext(after conflict: Conflict) {
        guard let index = model.conflicts.firstIndex(of: conflict),
              index + 1 < model.conflicts.count else { return }
        selection = model.conflicts[index + 1].id
    }

    // MARK: Apply

    private var applyBar: some View {
        HStack {
            Text(model.isWorking && model.isSyncing
                 ? "Waiting for the running sync to finish, then your decisions are applied…"
                 : "Overwritten versions are kept in the sync trash. Merges are only applied after you approve them.")
                .font(.caption).foregroundStyle(.secondary)
            Spacer()
            if model.isWorking { ProgressView().controlSize(.small) }
            Button("Apply \(decisions.count) Decision(s)") {
                let batch = model.conflicts.compactMap { conflict in
                    decisions[conflict.id].map {
                        ConflictDecision(pair: conflict.pair, file: conflict.file, keep: $0,
                                         mergedText: $0 == "merged" ? mergedTexts[conflict.id] : nil)
                    }
                }
                let outcomes = model.conflicts.compactMap { conflict -> AICorrection? in
                    guard let user = decisions[conflict.id], let ai = model.aiSuggestions[conflict.id] else { return nil }
                    return AICorrection(file: conflict.file, suggested: ai, userChoice: user)
                }
                Task {
                    result = await model.resolve(batch)
                    let open = Set(model.conflicts.map(\.file))
                    let applied = outcomes.filter { !open.contains($0.file) }
                    AITrust.shared.record(applied.map { ($0.file, $0.suggested.keep, $0.userChoice) })
                    corrections = applied.filter { $0.suggested.keep != "ask" && $0.suggested.keep != $0.userChoice }
                    // Keep the decisions for conflicts that are still open (e.g. after an error).
                    decisions = decisions.filter { id, _ in model.conflicts.contains { $0.id == id } }
                }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(decisions.isEmpty || model.isWorking)
        }
        .padding(10)
    }
}

/// An AI merge waiting for the user's approval.
struct MergeProposal: Identifiable {
    let id: Conflict.ID
    let file: String
    let local: String
    let onedrive: String
    let merged: String
}

/// Shows both versions next to the AI merge; nothing is written until "Use This Merge".
struct MergeReviewView: View {
    let proposal: MergeProposal
    let onAccept: (String) -> Void
    let onCancel: () -> Void
    @State private var merged = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Review the merge").font(.title3.bold())
            Text(proposal.file).foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
            HSplitView {
                column("Laptop", text: .constant(proposal.local), editable: false)
                column("OneDrive", text: .constant(proposal.onedrive), editable: false)
                column("Merged (you can edit it)", text: $merged, editable: true)
            }
            .frame(minHeight: 380)
            HStack {
                Text("Both old versions are kept in the sync trash when the merge is applied.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Cancel", action: onCancel).keyboardShortcut(.cancelAction)
                Button("Use This Merge") { onAccept(merged) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(merged.isEmpty)
            }
        }
        .padding()
        .frame(minWidth: 1100, minHeight: 520)
        .onAppear { merged = proposal.merged }
    }

    @ViewBuilder private func column(_ title: String, text: Binding<String>, editable: Bool) -> some View {
        VStack(alignment: .leading) {
            Text(title).font(.subheadline.bold())
            if editable {
                TextEditor(text: text).font(.system(.caption, design: .monospaced))
            } else {
                ScrollView {
                    Text(text.wrappedValue)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .frame(minWidth: 280)
    }
}
