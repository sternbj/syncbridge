import AppKit
import SwiftUI

/// Folder pairs as a list with a detail editor (sync variant, directions, filter).
struct FoldersSettingsView: View {
    @EnvironmentObject var model: AppModel
    @Binding var pairs: [ConfigDraft.Pair]
    @State private var selection: ConfigDraft.Pair.ID?

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                List(selection: $selection) {
                    ForEach(pairs) { pair in
                        PairRow(pair: pair, info: model.status?.pairs.first { $0.name == pair.name })
                            .tag(pair.id)
                    }
                }
                Divider()
                HStack(spacing: 0) {
                    Button { addPair() } label: { Image(systemName: "plus").frame(width: 24, height: 20) }
                    Button { removeSelected() } label: { Image(systemName: "minus").frame(width: 24, height: 20) }
                        .disabled(selection == nil)
                    Spacer()
                }
                .buttonStyle(.borderless)
                .padding(4)
            }
            .frame(width: 220)
            Divider()
            if let index = pairs.firstIndex(where: { $0.id == selection }) {
                PairEditor(pair: $pairs[index])
            } else {
                Text(pairs.isEmpty ? "Add a folder pair with +" : "Select a folder pair")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear { if selection == nil { selection = pairs.first?.id } }
    }

    /// Picks a local folder and suggests the cloud folder of the same name
    /// next to the existing pairs.
    private func addPair() {
        guard let local = chooseFolder(title: "Choose the local folder to sync") else { return }
        let name = (local as NSString).lastPathComponent
        let parents = pairs.map { ($0.onedrive as NSString).deletingLastPathComponent }
        let parent = Dictionary(grouping: parents, by: { $0 }).max { $0.value.count < $1.value.count }?.key
            ?? (CloudAccounts.all.first.map { ($0.path as NSString).abbreviatingWithTildeInPath } ?? "")
        var pair = ConfigDraft.Pair(name: name, local: local, onedrive: (parent as NSString).appendingPathComponent(name))
        pair.enabled = true
        pairs.append(pair)
        selection = pair.id
    }

    private func removeSelected() {
        pairs.removeAll { $0.id == selection }
        selection = pairs.first?.id
    }
}

private struct PairRow: View {
    let pair: ConfigDraft.Pair
    let info: PairInfo?

    var body: some View {
        HStack {
            Circle().fill(color).frame(width: 8, height: 8)
            VStack(alignment: .leading, spacing: 1) {
                Text(pair.name.isEmpty ? "Unnamed" : pair.name).lineLimit(1)
                Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
        }
    }

    private var color: Color {
        if !pair.enabled { return .gray }
        if (info?.conflicts ?? 0) > 0 || (info?.pendingDeletions ?? 0) > 0 { return .orange }
        return .green
    }

    private var subtitle: String {
        if !pair.enabled { return "Paused" }
        var parts = [PairEditor.variantNames[pair.variant] ?? pair.variant]
        if let conflicts = info?.conflicts, conflicts > 0 { parts.append("\(conflicts) conflicts") }
        if let pending = info?.pendingDeletions, pending > 0 { parts.append("\(pending) deletions") }
        return parts.joined(separator: " · ")
    }
}

struct PairEditor: View {
    @EnvironmentObject var model: AppModel
    @Binding var pair: ConfigDraft.Pair
    @State private var refresh = 0

    static let variantNames = ["two-way": "Two way", "mirror": "Mirror", "update": "Update", "custom": "Custom"]
    private static let variantHelp = [
        "two-way": "Changes, new files and deletions on either side are copied to the other side. A file changed on both sides is a conflict.",
        "mirror": "Makes the target an exact copy of the source. Changes made on the target are overwritten or removed.",
        "update": "Copies new and changed files from the source. Nothing is ever deleted on the target.",
        "custom": "Choose what happens for each kind of change.",
    ]
    private static let categoryNames = [
        "local_new": "New locally", "local_changed": "Changed locally", "local_deleted": "Deleted locally",
        "onedrive_new": "New in cloud", "onedrive_changed": "Changed in cloud", "onedrive_deleted": "Deleted in cloud",
    ]

    var body: some View {
        Form {
            Section {
                TextField("Name", text: $pair.name)
                Toggle("Active (turn off to pause this pair)", isOn: $pair.enabled)
            }
            Section("Folders") {
                folderRow("Local", path: $pair.local, display: pair.local, places: [])
                folderRow("Cloud or drive", path: $pair.onedrive, display: CloudAccounts.display(pair.onedrive),
                          places: [("Cloud Folder…", CloudAccounts.all.first ?? CloudAccounts.root),
                                   ("Folder on a USB Stick or Drive…", ExternalDrives.root)])
            }
            Section("Synchronization") {
                Picker("Variant", selection: $pair.variant) {
                    ForEach(["two-way", "mirror", "update", "custom"], id: \.self) {
                        Text(Self.variantNames[$0] ?? $0).tag($0)
                    }
                }
                .pickerStyle(.segmented)
                Text(Self.variantHelp[pair.variant] ?? "").font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if ["mirror", "update"].contains(pair.variant) {
                    Picker("Direction", selection: $pair.direction) {
                        Text("Local → Cloud").tag("to_onedrive")
                        Text("Cloud → Local").tag("to_local")
                    }
                }
                if pair.variant == "custom" {
                    ForEach(ConfigDraft.Pair.categories, id: \.self) { category in
                        Picker(Self.categoryNames[category] ?? category, selection: directionBinding(category)) {
                            Text("Copy to cloud").tag("to_onedrive")
                            Text("Copy to local").tag("to_local")
                            Text("Do nothing").tag("none")
                        }
                    }
                    Text("“Copy” makes the other side match, so a deletion is passed on and a change on the target side is undone.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Filter") {
                filterEditor("Include (empty = everything)", text: $pair.include)
                filterEditor("Exclude", text: $pair.exclude)
                LabeledContent("File size") {
                    HStack {
                        TextField("min", value: $pair.minSizeMB, format: .number).frame(width: 70)
                        Text("to")
                        TextField("max", value: $pair.maxSizeMB, format: .number).frame(width: 70)
                        Text("MB (0 = no limit)").foregroundStyle(.secondary)
                    }
                }
                LabeledContent("Changed within") {
                    HStack {
                        TextField("days", value: $pair.maxAgeDays, format: .number).frame(width: 70)
                        Text("days (0 = any time)").foregroundStyle(.secondary)
                    }
                }
                Text("Patterns are relative to the pair, e.g. Projects/** or *.mov. Filtered files are left alone on both sides, never deleted.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                HStack {
                    Button("Sync This Pair Now") { Task { await model.syncNow(pairs: [pair.name]) } }
                        .disabled(model.isSyncing || !pair.enabled)
                    Text("Uses the saved settings.").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .id(refresh)
    }

    private func directionBinding(_ category: String) -> Binding<String> {
        Binding(
            get: { pair.directions[category] ?? "none" },
            set: { pair.directions[category] = $0 }
        )
    }

    /// places: where the folder picker can start; none means a plain "Choose…" button.
    private func folderRow(
        _ label: String, path: Binding<String>, display: String, places: [(title: String, start: URL)]
    ) -> some View {
        let exists = FileManager.default.fileExists(atPath: (path.wrappedValue as NSString).expandingTildeInPath)
        let choose = { (start: URL?) in
            if let chosen = chooseFolder(title: "Choose the \(label.lowercased()) folder", startIn: start) {
                path.wrappedValue = chosen
            }
        }
        return LabeledContent(label) {
            VStack(alignment: .trailing, spacing: 4) {
                HStack {
                    Text(path.wrappedValue.isEmpty ? "Not set" : display)
                        .lineLimit(1).truncationMode(.middle)
                        .foregroundStyle(path.wrappedValue.isEmpty ? .red : .primary)
                        .help(path.wrappedValue)
                    if places.isEmpty {
                        Button("Choose…") { choose(nil) }
                    } else {
                        Menu("Choose…") {
                            ForEach(places, id: \.title) { place in
                                Button(place.title) { choose(place.start) }
                            }
                        }
                        .fixedSize()
                    }
                    Button {
                        NSWorkspace.shared.open(URL(fileURLWithPath: (path.wrappedValue as NSString).expandingTildeInPath))
                    } label: { Image(systemName: "arrow.right.circle") }
                        .buttonStyle(.borderless)
                        .help("Show in Finder")
                        .disabled(!exists)
                }
                if !path.wrappedValue.isEmpty && !exists && ExternalDrives.isDisconnected(path.wrappedValue) {
                    Text("Drive \"\(ExternalDrives.name(of: path.wrappedValue) ?? "")\" is not connected. "
                         + "This pair waits and syncs when you plug it in.")
                        .font(.caption).foregroundStyle(.secondary)
                } else if !path.wrappedValue.isEmpty && !exists {
                    HStack {
                        Text("Folder does not exist").font(.caption).foregroundStyle(.orange)
                        Button("Create") {
                            try? FileManager.default.createDirectory(
                                atPath: (path.wrappedValue as NSString).expandingTildeInPath,
                                withIntermediateDirectories: true
                            )
                            refresh += 1
                        }
                        .controlSize(.small)
                    }
                }
            }
        }
    }

    private func filterEditor(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading) {
            Text(title)
            TextEditor(text: text)
                .font(.system(.body, design: .monospaced))
                .frame(height: 44)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
        }
    }
}

func chooseFolder(title: String, startIn: URL? = nil) -> String? {
    let panel = NSOpenPanel()
    panel.message = title
    panel.canChooseDirectories = true
    panel.canChooseFiles = false
    panel.canCreateDirectories = true
    panel.directoryURL = startIn
    guard panel.runModal() == .OK, let url = panel.url else { return nil }
    return (url.path as NSString).abbreviatingWithTildeInPath
}
