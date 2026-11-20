import SwiftUI

/// AI help for conflicts: provider, model and API key. Saved right away;
/// the key goes to the Keychain.
struct AISettingsView: View {
    @AppStorage(AIAssistant.providerKey) private var providerRaw = AIProvider.off.rawValue
    @State private var model = ""
    @State private var key = ""
    @State private var hasKey = false
    @State private var testResult: String?
    @State private var testing = false
    @State private var chatgptEmail: String?
    @State private var chatgptModels: [ChatGPTModel] = []
    @State private var signingIn = false
    @State private var signInCancelled = false
    @State private var claude: ClaudeCodeStatus?
    @State private var claudeAccounts: [ClaudeCodeStatus] = []
    @AppStorage(ClaudeCode.configFolderKey) private var claudeFolder = ""

    private var provider: AIProvider { AIProvider(rawValue: providerRaw) ?? .off }

    var body: some View {
        Form {
            Section {
                Picker("AI provider", selection: $providerRaw) {
                    ForEach(AIProvider.allCases) { Text($0.label).tag($0.rawValue) }
                }
                Text("""
                AI help suggests which version to keep, explains what each side changed and can propose \
                a merge for text files. Nothing is changed until you approve it, unless you later turn on \
                automatic decisions below.
                """)
                .font(.caption).foregroundStyle(.secondary)
            }
            if provider == .chatgpt { chatgptSection }
            if provider == .claudeCode { claudeSection }
            if provider == .apple {
                Section("Apple on-device model") {
                    LabeledContent("Status", value: AIAssistant.appleModelStatus)
                    Text("Free and private: your files never leave this Mac. Its context is small, so long files are shortened.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if provider.needsKey {
                Section(provider.label) {
                    TextField("Model", text: $model, prompt: Text(provider.modelHint))
                        .onSubmit(saveModel)
                    SecureField(hasKey ? "API key (saved — enter a new one to replace it)" : "API key", text: $key)
                        .onSubmit(saveKey)
                    HStack {
                        Link("Get an API key", destination: URL(string: provider.keyURL)!)
                        Spacer()
                        if hasKey { Button("Remove Key") { KeychainStore.setKey("", for: provider); refresh() } }
                        Button("Save") { saveModel(); saveKey() }
                    }
                    Text("""
                    Uses your own API key and is billed by \(provider.label). The key is stored in the macOS \
                    Keychain. When you ask for help, the text of both versions of that one file is sent to \
                    \(provider.label). A ChatGPT Plus or Claude Pro subscription can't be used here; it needs an API key.
                    """)
                    .font(.caption).foregroundStyle(.secondary)
                }
            }
            if provider != .off {
                Section {
                    HStack {
                        Button("Test Connection") { Task { await test() } }.disabled(testing)
                        if testing { ProgressView().controlSize(.small) }
                    }
                    if let testResult { Text(testResult).font(.caption).textSelection(.enabled) }
                }
                AIInstructionsView(provider: provider)
            }
        }
        .formStyle(.grouped)
        .onAppear(perform: refresh)
        .onChange(of: providerRaw) { _ in refresh() }
    }

    // MARK: Subscriptions

    private var chatgptSection: some View {
        Section("ChatGPT plan (Go, Plus or Pro)") {
            if let email = chatgptEmail {
                LabeledContent("Connected", value: email.isEmpty ? "Yes" : email)
                Picker("Model", selection: $model) {
                    Text("Automatic").tag("")
                    ForEach(chatgptModels) { Text($0.name).tag($0.id) }
                    if !model.isEmpty, !chatgptModels.contains(where: { $0.id == model }) { Text(model).tag(model) }
                }
                .onChange(of: model) { _ in saveModel() }
                HStack {
                    Link("Usage limits", destination: ChatGPTAccount.usageURL)
                    Spacer()
                    Button("Sign Out") {
                        Task { await ChatGPTAccount.shared.signOut(); refresh() }
                    }
                }
            } else {
                HStack {
                    Button("Continue with ChatGPT") { Task { await signInChatGPT() } }
                        .disabled(signingIn)
                    if signingIn {
                        ProgressView().controlSize(.small)
                        Text("Finish signing in in your browser…").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button("Cancel") {
                            signInCancelled = true
                            Task { await ChatGPTAccount.shared.cancelSignIn() }
                        }
                    }
                }
                if signingIn {
                    HStack {
                        Text("Wrong browser or profile, or closed the page? Copy the link into the right one.")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button("Open Again") { withSignInPage { NSWorkspace.shared.open($0) } }
                        Button("Copy Link") {
                            withSignInPage { page in
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(page.absoluteString, forType: .string)
                            }
                        }
                    }
                    .controlSize(.small)
                }
            }
            Text("""
            Uses your own ChatGPT plan through OpenAI's "Sign in with ChatGPT"; requests count toward \
            your plan's limits. The sign-in is stored in the macOS Keychain. You can remove SyncBridge's \
            access any time in ChatGPT under Settings › Security and login.
            """)
            .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var claudeSection: some View {
        Section("Claude plan (Pro or Max) via Claude Code") {
            if let claude {
                if claude.path == nil {
                    Text("Claude Code isn't installed.").foregroundStyle(.orange)
                    Link("How to install Claude Code", destination: URL(string: "https://docs.claude.com/en/docs/claude-code/setup")!)
                } else {
                    LabeledContent("Claude Code", value: claude.version.isEmpty ? "Installed" : claude.version)
                    if claude.loggedIn {
                        LabeledContent("Signed in", value: claude.usesPlan
                                       ? "with your Claude plan\(claude.plan.isEmpty ? "" : " (\(claude.plan))")"
                                       : "with an API key (billed per use)")
                    } else {
                        Text("Not signed in. Open Terminal, run `claude`, and sign in with your Claude account.")
                            .foregroundStyle(.orange)
                    }
                    if claudeAccounts.count > 1 || (!claudeAccounts.isEmpty && !claude.loggedIn) {
                        Picker("Claude account", selection: $claudeFolder) {
                            ForEach(claudeAccounts, id: \.folder) { account in
                                Text(Self.accountLabel(account)).tag(account.folder)
                            }
                            if !claudeAccounts.contains(where: { $0.folder == claudeFolder }) {
                                Text(claudeFolder.isEmpty ? "Default (~/.claude)" : claudeFolder).tag(claudeFolder)
                            }
                        }
                        .onChange(of: claudeFolder) { _ in Task { await checkClaude() } }
                    } else if !claude.folder.isEmpty {
                        LabeledContent("Account", value: Self.accountLabel(claude))
                    }
                }
            } else {
                ProgressView().controlSize(.small)
            }
            Picker("Model", selection: $model) {
                ForEach(ClaudeCode.models, id: \.id) { Text($0.label).tag($0.id) }
            }
            .onChange(of: model) { _ in saveModel() }
            HStack {
                Spacer()
                Button("Check Again") { Task { await checkClaude() } }
            }
            Text("""
            SyncBridge runs your own Claude Code with all tools switched off and never sees your Claude \
            login. Usage counts toward your Claude plan's limits.
            """)
            .font(.caption).foregroundStyle(.secondary)
        }
    }

    static func accountLabel(_ account: ClaudeCodeStatus) -> String {
        let folder = account.folder.replacingOccurrences(of: NSHomeDirectory(), with: "~")
        let who = [account.email, account.plan].filter { !$0.isEmpty }.joined(separator: ", ")
        return who.isEmpty ? folder : "\(folder) — \(who)"
    }

    /// Finds signed-in accounts; if the chosen one isn't signed in and exactly one other is, uses that.
    private func checkClaude() async {
        claude = nil
        claudeAccounts = await ClaudeCode.accounts()
        var current = await ClaudeCode.status(configFolder: claudeFolder)
        if !current.loggedIn, claudeAccounts.count == 1, let only = claudeAccounts.first {
            claudeFolder = only.folder
            current = only
        }
        claude = current
    }

    private func withSignInPage(_ action: @escaping @MainActor (URL) -> Void) {
        Task {
            if let page = await ChatGPTAccount.shared.signInPage { action(page) }
        }
    }

    private func signInChatGPT() async {
        signingIn = true
        signInCancelled = false
        defer { signingIn = false }
        do {
            _ = try await ChatGPTAccount.shared.signIn()
            testResult = nil
        } catch {
            testResult = signInCancelled ? nil : "Sign-in failed: \(error.localizedDescription)"
        }
        refresh(keepResult: true)
    }

    private func refresh() { refresh(keepResult: false) }

    private func refresh(keepResult: Bool) {
        model = AIAssistant.model(for: provider)
        key = ""
        hasKey = KeychainStore.key(for: provider) != nil
        if !keepResult { testResult = nil }
        chatgptEmail = ChatGPTAccount.session?.email
        if provider == .chatgpt, chatgptEmail != nil {
            Task { chatgptModels = (try? await ChatGPTAccount.shared.models()) ?? [] }
        }
        if provider == .claudeCode, claude == nil {
            Task { await checkClaude() }
        }
    }

    private func saveModel() {
        UserDefaults.standard.set(model.trimmingCharacters(in: .whitespaces), forKey: AIAssistant.modelKeyPrefix + provider.rawValue)
    }

    private func saveKey() {
        guard !key.isEmpty else { return }
        KeychainStore.setKey(key, for: provider)
        key = ""
        hasKey = KeychainStore.key(for: provider) != nil
    }

    private func test() async {
        saveModel()
        saveKey()
        testing = true
        defer { testing = false }
        do {
            let answer = try await AIAssistant(provider: provider).complete(
                system: "Reply with exactly: OK", prompt: "Connection test"
            )
            testResult = "Connected. The model answered: \(answer.prefix(80))"
        } catch {
            testResult = "Failed: \(error.localizedDescription)"
        }
    }
}
