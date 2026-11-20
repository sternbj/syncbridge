import SwiftUI

/// FreeFileSync-style Compare: shows what the next sync would do and lets you
/// change the action per file before synchronizing.
struct PreviewView: View {
    @EnvironmentObject var model: AppModel
    @State private var overrides: [String: String] = [:]
    @State private var category = Category.all

    enum Category: String, CaseIterable, Identifiable {
        case all = "All", toOneDrive = "→ OneDrive", toLocal = "→ Local", deletions = "Deletions", conflicts = "Conflicts"
        var id: String { rawValue }
    }

    struct Row: Identifiable, Hashable {
        let pair: String
        let file: String
        let planned: String
        let detail: String
        var id: String { "\(pair)|\(file)" }
    }

    static let actionLabels = [
        "copy_local_to_onedrive": "Copy → OneDrive",
        "copy_onedrive_to_local": "Copy → Local",
        "delete_onedrive": "Delete in OneDrive",
        "delete_local": "Delete locally",
        "rename_onedrive": "Rename in OneDrive",
        "rename_local": "Rename locally",
        "create_folder_onedrive": "Create folder in OneDrive",
        "create_folder_local": "Create folder locally",
        "delete_folder_onedrive": "Remove empty folder in OneDrive",
        "delete_folder_local": "Remove empty folder locally",
        "conflict": "Conflict",
        "skip": "Skip",
    ]
    private static let choices = ["copy_local_to_onedrive", "copy_onedrive_to_local", "delete_onedrive", "delete_local", "skip"]

    private var rows: [Row] {
        guard let preview = model.preview else { return [] }
        let actions = preview.actions.map { action in
            Row(
                pair: action.pair, file: action.file, planned: action.action,
                detail: action.held == true ? "held for review" : (action.from.map { "from \($0)" } ?? "")
            )
        }
        let conflicts = preview.conflicts.map { Row(pair: $0.pair, file: $0.file, planned: "conflict", detail: $0.reason) }
        return (actions + conflicts).filter(matches)
    }

    private func matches(_ row: Row) -> Bool {
        switch category {
        case .all: return true
        case .toOneDrive: return ["copy_local_to_onedrive", "create_folder_onedrive"].contains(row.planned)
        case .toLocal: return ["copy_onedrive_to_local", "create_folder_local"].contains(row.planned)
        case .deletions: return row.planned.hasPrefix("delete_")
        case .conflicts: return row.planned == "conflict"
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button { Task { await compare() } } label: { Label("Compare", systemImage: "magnifyingglass") }
                    .disabled(model.isComparing || model.isSyncing)
                Picker("", selection: $category) {
                    ForEach(Category.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                Spacer()
                if model.isComparing || model.isSyncing { ProgressView().controlSize(.small) }
            }
            .padding(10)
            Divider()
            if let skipped = model.preview?.skippedPairs, !skipped.isEmpty {
                Text("Not compared: " + skipped.map { "\($0.pair) (\($0.reason ?? "error"))" }.joined(separator: "; "))
                    .font(.caption).foregroundStyle(.orange).padding(6)
            }
            table
            Divider()
            HStack {
                Text(summary).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Reset Choices") { overrides.removeAll() }.disabled(overrides.isEmpty)
                Button("Synchronize") { Task { await synchronize() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.preview == nil || model.isSyncing || model.isComparing)
            }
            .padding(10)
        }
        .task { if model.preview == nil { await compare() } }
    }

    private var table: some View {
        Table(rows) {
            TableColumn("Pair", value: \.pair).width(min: 60, ideal: 90, max: 140)
            TableColumn("File") { row in
                Text(row.file).lineLimit(1).truncationMode(.head).help(row.file)
            }
            TableColumn("Planned") { row in
                VStack(alignment: .leading) {
                    Text(Self.actionLabels[row.planned] ?? row.planned)
                        .foregroundStyle(row.planned == "conflict" ? .orange : .primary)
                    if !row.detail.isEmpty {
                        Text(row.detail).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .width(min: 120, ideal: 170)
            TableColumn("Your Choice") { row in
                // Renames and folder actions follow their files; they have no per-row choice.
                if row.planned.hasPrefix("rename_") || row.planned.contains("folder") {
                    Text("—").foregroundStyle(.secondary)
                } else {
                    Picker("", selection: choiceBinding(row)) {
                        Text(row.planned == "conflict" ? "Leave open" : "As planned").tag("")
                        ForEach(Self.choices, id: \.self) { Text(Self.actionLabels[$0] ?? $0).tag($0) }
                    }
                    .labelsHidden()
                }
            }
            .width(min: 150, ideal: 170)
        }
    }

    private func choiceBinding(_ row: Row) -> Binding<String> {
        Binding(
            get: { overrides[row.id] ?? "" },
            set: { overrides[row.id] = $0.isEmpty ? nil : $0 }
        )
    }

    private var summary: String {
        guard let preview = model.preview else { return "Click Compare to see what the next sync would do." }
        let held = preview.actions.filter { $0.held == true }.count
        var parts = ["\(preview.actions.count - held) change(s)"]
        if held > 0 { parts.append("\(held) deletion(s) held for review") }
        if !preview.conflicts.isEmpty { parts.append("\(preview.conflicts.count) conflict(s)") }
        if !overrides.isEmpty { parts.append("\(overrides.count) choice(s) of yours") }
        return parts.joined(separator: " · ")
    }

    private func compare() async {
        overrides.removeAll()
        await model.loadPreview()
    }

    private func synchronize() async {
        let chosen = overrides.compactMap { id, action -> PreviewOverride? in
            let parts = id.split(separator: "|", maxSplits: 1).map(String.init)
            return parts.count == 2 ? PreviewOverride(pair: parts[0], file: parts[1], action: action) : nil
        }
        await model.syncNow(overrides: chosen)
        await compare()
    }
}
