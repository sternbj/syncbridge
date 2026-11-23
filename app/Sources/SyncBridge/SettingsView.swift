import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var draft = ConfigDraft()
    @State private var loaded = false
    @State private var errors: [String] = []
    @State private var saved = false
    @State private var startAtLogin = LoginItem.isEnabled
    @AppStorage(Engine.pythonDefaultsKey) private var pythonPath = ""
    @AppStorage(AppModel.realtimeKey) private var realtime = false
    @AppStorage(AppModel.realtimeDelayKey) private var realtimeDelay = 30

    var body: some View {
        VStack(spacing: 0) {
            NavigationSplitView {
                List(Self.sections, id: \.tag, selection: Binding(
                    get: { Optional(model.settingsTab) },
                    set: { if let tag = $0 { model.settingsTab = tag } }
                )) { section in
                    Label(section.title, systemImage: section.icon).tag(section.tag)
                }
                .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 260)
            } detail: {
                // A fresh page per section: reusing one sometimes left the detail column blank.
                page(model.settingsTab)
                    .formStyle(.grouped)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    .id(model.settingsTab)
            }
            Divider()
            HStack {
                OpenConflictsButton()
                if !errors.isEmpty {
                    Text(errors.joined(separator: "\n")).foregroundStyle(.red).font(.caption)
                } else if saved {
                    Text("Saved. Changes apply from the next sync.").foregroundStyle(.secondary).font(.caption)
                }
                Spacer()
                Button("Revert") { Task { await load() } }
                Button("Save") { Task { await save() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!loaded)
            }
            .padding(10)
        }
        .frame(minWidth: 760, minHeight: 480)
        .task { await load() }
        .modifier(StarPromptModifier())
    }

    // MARK: Sections

    static let sections: [(tag: String, title: String, icon: String)] = [
        ("folders", "Folders", "folder"),
        ("conflicts", "Conflicts", "arrow.triangle.branch"),
        ("deletions", "Deletions & Versions", "trash"),
        ("exclusions", "Exclusions", "eye.slash"),
        ("ai", "AI", "sparkles"),
        ("general", "Schedule & General", "gearshape"),
        ("feedback", "Help & Feedback", "bubble.left.and.bubble.right"),
    ]

    @ViewBuilder private func page(_ tag: String) -> some View {
        switch tag {
        case "conflicts": conflictsTab
        case "deletions": deletionsTab
        case "exclusions": exclusionsTab
        case "ai": AISettingsView()
        case "general": generalTab
        case "feedback": FeedbackSettingsView()
        default: FoldersSettingsView(pairs: $draft.pairs)
        }
    }

    private var conflictsTab: some View {
        Form {
            Section {
                HStack {
                    Text(openConflictsText)
                    Spacer()
                    OpenConflictsButton()
                }
            }
            RulesSettingsView(draft: $draft)
            Section("Everything else") {
                Picker("When a file changed on both sides", selection: $draft.conflictRule) {
                    Text("Ask me").tag("ask")
                    Text("Resolve the safe cases, ask me about the rest").tag("safe")
                    Text("Keep the newer version").tag("newer")
                    Text("Always keep the local version").tag("local")
                    Text("Always keep the cloud version").tag("onedrive")
                }
                .pickerStyle(.radioGroup)
                Text("""
                Applies to two-way sync when no rule above decides. The version that is overwritten \
                is kept as set under Deletions & Versions (not with "Delete permanently"). If one side was deleted \
                and the other edited, "newer" keeps the edited file; a rule never deletes a file. \
                "Safe cases" are: one version is empty or damaged, a picture or video is an interrupted \
                copy, the newer one has the same content (an Office file only saved again, a text file \
                with other line endings), or a plain text file only got new lines. Name conflicts and edits \
                made within the same second always wait for you, and a date in the future (a wrong clock) \
                never counts as newer. Mirror and update \
                pairs have no conflicts: their direction decides.
                """)
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var openConflictsText: String {
        let count = model.status?.conflicts ?? 0
        return count == 0 ? "No open conflicts." : "\(count) conflict(s) wait for your decision."
    }

    private var deletionsTab: some View {
        Form {
            Section("Safety") {
                Stepper(value: $draft.autoDeleteLimit, in: 0...10_000, step: 10) {
                    Text("Apply up to \(draft.autoDeleteLimit) new deletions per sync automatically")
                }
                Text("Larger batches wait in Pending Deletions. Set to 0 to always review deletions.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Deleted and overwritten files") {
                Picker("Handling", selection: $draft.deletionHandling) {
                    Text("Versioning (keep in a SyncBridge folder)").tag("versioning")
                    Text("Move to the macOS Trash").tag("trash")
                    Text("Delete permanently").tag("permanent")
                }
                .pickerStyle(.radioGroup)
                Text("Cloud deletions also land in the cloud provider's recycle bin.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if draft.deletionHandling == "versioning" {
                Section("Versioning") {
                    LabeledContent("Folder") {
                        HStack {
                            Text(draft.versioningFolder.isEmpty ? "Default (SyncBridge state folder)" : draft.versioningFolder)
                                .lineLimit(1).truncationMode(.middle)
                            Button("Choose…") {
                                if let folder = chooseFolder(title: "Choose the versioning folder") {
                                    draft.versioningFolder = folder
                                }
                            }
                            if !draft.versioningFolder.isEmpty {
                                Button("Default") { draft.versioningFolder = "" }
                            }
                        }
                    }
                    Picker("Naming", selection: $draft.versioningStyle) {
                        Text("Folder per sync (2026-11-16_103000/…)").tag("timestamp_folder")
                        Text("Time stamp in file name (Report 2026-11-16_103000.docx)").tag("timestamp_file")
                        Text("Replace (keep the last version only)").tag("replace")
                    }
                    Stepper(value: $draft.trashRetentionDays, in: 0...3650) {
                        Text(draft.trashRetentionDays == 0
                             ? "Keep versions forever"
                             : "Remove versions older than \(draft.trashRetentionDays) days")
                    }
                    Stepper(value: $draft.versioningMaxCount, in: 0...1000) {
                        Text(draft.versioningMaxCount == 0
                             ? "No maximum number of versions per file"
                             : "Keep at most \(draft.versioningMaxCount) versions per file")
                    }
                    Stepper(value: $draft.versioningMinCount, in: 0...1000) {
                        Text("Always keep at least \(draft.versioningMinCount) versions per file")
                    }
                    .disabled(draft.trashRetentionDays == 0)
                }
            }
        }
        .formStyle(.grouped)
    }

    private var exclusionsTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("Apply to all pairs. One entry per line, wildcards like * are allowed. Built-in exclusions (.git, node_modules, .DS_Store, app bundles …) always apply. Each pair can add its own filter under Folders.")
                    .font(.caption).foregroundStyle(.secondary)
                exclusionEditor("Folder names (anywhere)", text: $draft.excludeDirs)
                exclusionEditor("Path patterns (relative to the pair)", text: $draft.excludePatterns)
                exclusionEditor("File names", text: $draft.excludeNames)
            }
        }
    }

    private func exclusionEditor(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading) {
            Text(title).bold()
            TextEditor(text: text)
                .font(.system(.body, design: .monospaced))
                .frame(minHeight: 70)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.secondary.opacity(0.3)))
        }
    }

    private var generalTab: some View {
        Form {
            Section("Schedule") {
                Picker("Sync every", selection: $draft.syncIntervalMinutes) {
                    ForEach(AppModel.intervalChoices, id: \.minutes) { Text($0.label).tag($0.minutes) }
                }
                Toggle("Also sync when files change (real time)", isOn: $realtime)
                    .onChange(of: realtime) { _ in model.updateWatcher() }
                if realtime {
                    Stepper(value: $realtimeDelay, in: 5...600, step: 5) {
                        Text("Wait until nothing changed for \(Duration.seconds(realtimeDelay).formatted(.units(allowed: [.minutes, .seconds], width: .wide)))")
                    }
                }
            }
            Section("After each sync") {
                TextField("Run command (optional)", text: $draft.runAfterSync)
                Text("Runs in a shell; $SYNCBRIDGE_STATUS is the path of the status file.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("App") {
                Toggle("Start SyncBridge at login", isOn: $startAtLogin)
                    .onChange(of: startAtLogin) { enabled in
                        do { try LoginItem.setEnabled(enabled) } catch { errors = [error.localizedDescription] }
                    }
                TextField("Python 3 path (empty = built-in)", text: $pythonPath)
                if let python = model.engine.pythonPath {
                    Text("Using \(python)").font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
    }

    // MARK: Load / save

    private func load() async {
        do {
            draft = ConfigDraft(raw: try await model.engine.loadConfig())
            loaded = true
            errors = []
            saved = false
        } catch {
            errors = [error.localizedDescription]
        }
    }

    private func save() async {
        do {
            errors = try await model.engine.saveConfig(draft.payload)
            saved = errors.isEmpty
            if saved {
                await model.refreshStatus()
                model.scheduleSyncs()
            }
        } catch {
            errors = [error.localizedDescription]
        }
    }
}
