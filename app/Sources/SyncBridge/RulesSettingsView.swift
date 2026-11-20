import SwiftUI

/// Fixed rules on the Conflicts page: per-folder rules and author priority.
/// The engine applies them exactly, before the general conflict setting.
struct RulesSettingsView: View {
    @Binding var draft: ConfigDraft
    @State private var problem: String?

    var body: some View {
        Section {
            ForEach($draft.folderRules) { $rule in
                HStack {
                    TextField("Folder pattern", text: $rule.pattern)
                        .font(.system(.body, design: .monospaced))
                    Picker("", selection: $rule.rule) {
                        ForEach(ConfigDraft.FolderRule.choices, id: \.rule) { Text($0.label).tag($0.rule) }
                    }
                    .labelsHidden()
                    .fixedSize()
                    Button {
                        draft.folderRules.removeAll { $0.id == rule.id }
                    } label: { Image(systemName: "minus.circle") }
                        .buttonStyle(.borderless)
                        .help("Remove this rule")
                }
            }
            HStack {
                Button("Add Folder…") { addFolder() }
                Button("Add Pattern") { draft.folderRules.append(.init(pattern: "", rule: "protect")) }
                if let problem { Text(problem).font(.caption).foregroundStyle(.orange) }
            }
        } header: {
            Text("Folder rules")
        } footer: {
            Text("""
            Paths are relative to the pair folders, e.g. Clients/** for everything in Clients. \
            The first matching rule counts. "Never delete" copies a deleted file back from the other \
            side, even in large batches. Conflict rules never delete a file and keep the overwritten \
            version as set under Deletions & Versions.
            """)
            .font(.caption).foregroundStyle(.secondary)
        }

        Section {
            TextEditor(text: $draft.authorPriority)
                .font(.system(.body, design: .monospaced))
                .frame(minHeight: 60)
        } header: {
            Text("Author priority (Word, Excel, PowerPoint)")
        } footer: {
            Text("""
            One name per line, highest rank first, written as Office shows the last editor \
            (e.g. "Doe, Jane"). When both versions were last saved by people on this list, the \
            higher-ranked person's version is kept. Other files, unknown names and deletions \
            follow the general setting above.
            """)
            .font(.caption).foregroundStyle(.secondary)
        }
    }

    /// Picks a folder inside one of the pairs and adds "relative/path/**".
    private func addFolder() {
        problem = nil
        let roots = draft.pairs.flatMap { [$0.local, $0.onedrive] }
            .map { ($0 as NSString).expandingTildeInPath }
        let start = roots.first.map { URL(fileURLWithPath: $0) }
        guard let chosen = chooseFolder(title: "Choose a folder inside a sync pair", startIn: start) else { return }
        let full = (chosen as NSString).expandingTildeInPath
        guard let root = roots.filter({ full.hasPrefix($0 + "/") }).max(by: { $0.count < $1.count }) else {
            problem = "That folder is not inside a sync pair."
            return
        }
        let relative = String(full.dropFirst(root.count + 1))
        draft.folderRules.append(.init(pattern: relative + "/**", rule: "protect"))
    }
}
