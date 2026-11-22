import Foundation
import Security
#if canImport(FoundationModels)
import FoundationModels
#endif

/// Where AI help for conflicts comes from. Cloud providers use the user's own API key.
enum AIProvider: String, CaseIterable, Identifiable {
    case off, chatgpt, claudeCode, apple, local, anthropic, openai, openrouter

    var id: String { rawValue }

    var label: String {
        switch self {
        case .off: return "Off"
        case .chatgpt: return "ChatGPT (subscription)"
        case .claudeCode: return "Claude (subscription, via Claude Code)"
        case .apple: return "Apple (on this Mac)"
        case .local: return "Local model (Ollama, LM Studio)"
        case .anthropic: return "Anthropic (API key)"
        case .openai: return "OpenAI (API key)"
        case .openrouter: return "OpenRouter (API key)"
        }
    }

    /// Short name for messages.
    var name: String {
        switch self {
        case .chatgpt: return "ChatGPT"
        case .claudeCode: return "Claude"
        case .apple: return "Apple's model"
        case .local: return "Your local model"
        case .anthropic: return "Anthropic"
        case .openai: return "OpenAI"
        case .openrouter: return "OpenRouter"
        case .off: return "AI"
        }
    }

    /// Name of the service that receives the text.
    var serviceName: String {
        switch self {
        case .chatgpt, .openai: return "OpenAI"
        case .claudeCode, .anthropic: return "Anthropic"
        case .openrouter: return "OpenRouter"
        case .local: return LocalModel.serverName
        default: return "nobody"
        }
    }

    /// How many conflicts to ask about at once. A model on this Mac shares one processor.
    var parallelRequests: Int { self == .local || self == .apple ? 1 : 3 }

    var needsKey: Bool { self == .anthropic || self == .openai || self == .openrouter }
    var isCloud: Bool { self != .off && self != .apple }

    var defaultModel: String {
        switch self {
        case .anthropic: return "claude-sonnet-5-5"
        default: return ""
        }
    }

    var modelHint: String {
        switch self {
        case .anthropic: return "e.g. claude-sonnet-5-5"
        case .openai: return "model name from your OpenAI account"
        case .openrouter: return "e.g. anthropic/… or openai/… (see openrouter.ai/models)"
        default: return ""
        }
    }

    var keyURL: String {
        switch self {
        case .anthropic: return "https://console.anthropic.com/settings/keys"
        case .openai: return "https://platform.openai.com/api-keys"
        case .openrouter: return "https://openrouter.ai/keys"
        default: return ""
        }
    }

    /// Apple's model has a small context window, so long files are cut shorter.
    /// Local models usually run with a context of a few thousand tokens.
    var maxCharsPerVersion: Int {
        switch self {
        case .apple: return 5_000
        case .local: return 6_000
        default: return 60_000
        }
    }
}

/// API keys and sign-in sessions live in the macOS Keychain, never in the config or UserDefaults.
enum KeychainStore {
    private static let service = "io.github.sternbj.syncbridge.ai"

    private static func query(_ account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    static func read(_ account: String) -> Data? {
        var lookup = query(account)
        lookup[kSecReturnData as String] = true
        lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(lookup as CFDictionary, &item) == errSecSuccess else { return nil }
        return item as? Data
    }

    /// Replaces the stored value; nil removes it.
    static func write(_ data: Data?, account: String) {
        SecItemDelete(query(account) as CFDictionary)
        guard let data, !data.isEmpty else { return }
        var item = query(account)
        item[kSecValueData as String] = data
        SecItemAdd(item as CFDictionary, nil)
    }

    static func key(for provider: AIProvider) -> String? {
        read(provider.rawValue).flatMap { String(data: $0, encoding: .utf8) }
    }

    static func setKey(_ key: String, for provider: AIProvider) {
        write(Data(key.trimmingCharacters(in: .whitespacesAndNewlines).utf8), account: provider.rawValue)
    }
}

enum AIError: LocalizedError {
    case notConfigured(String)
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured(let text), .failed(let text): return text
        }
    }
}

/// Sends prompts to the chosen provider.
struct AIAssistant {
    static let providerKey = "aiProvider"
    static let modelKeyPrefix = "aiModel."

    static var provider: AIProvider {
        AIProvider(rawValue: UserDefaults.standard.string(forKey: providerKey) ?? "") ?? .off
    }

    // MARK: Who decides conflicts: Jev when it is on, otherwise the provider above.

    static var canDecide: Bool { Jev.isOn || provider != .off }
    static var decisionParallelRequests: Int { Jev.isOn ? 5 : provider.parallelRequests }
    static var decisionMaxChars: Int { Jev.isOn ? Jev.maxCharsPerVersion : provider.maxCharsPerVersion }
    /// Who receives file text for decisions; nil when it stays on this Mac.
    static var decisionService: String? {
        Jev.isOn ? Jev.serviceName : provider.isCloud ? provider.serviceName : nil
    }

    /// Jev used to be a provider; it is now a separate switch next to one.
    static func migrate() {
        let defaults = UserDefaults.standard
        guard defaults.string(forKey: providerKey) == "jev" else { return }
        defaults.set(true, forKey: Jev.enabledKey)
        defaults.set(AIProvider.off.rawValue, forKey: providerKey)
    }

    static func model(for provider: AIProvider) -> String {
        let saved = UserDefaults.standard.string(forKey: modelKeyPrefix + provider.rawValue) ?? ""
        return saved.isEmpty ? provider.defaultModel : saved
    }

    static var appleModelStatus: String {
        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            switch SystemLanguageModel.default.availability {
            case .available: return "Available"
            case .unavailable(.deviceNotEligible): return "This Mac does not support Apple Intelligence."
            case .unavailable(.appleIntelligenceNotEnabled): return "Turn on Apple Intelligence in System Settings."
            case .unavailable(.modelNotReady): return "The model is still downloading. Try again later."
            case .unavailable: return "Not available on this Mac."
            }
        }
        #endif
        return "Needs macOS 26 or later."
    }

    let provider: AIProvider

    init(provider: AIProvider = AIAssistant.provider) {
        self.provider = provider
    }

    /// quick: the answer needs little thinking (a conflict decision); faster where the provider allows it.
    func complete(system: String, prompt: String, quick: Bool = false) async throws -> String {
        switch provider {
        case .off:
            throw AIError.notConfigured("AI help is off. Choose a provider in Settings › AI.")
        case .chatgpt:
            return try await ChatGPTAccount.shared.complete(
                system: system, prompt: prompt, model: Self.model(for: provider), quick: quick
            )
        case .claudeCode:
            return try await ClaudeCode.complete(system: system, prompt: prompt, model: Self.model(for: provider))
        case .apple:
            return try await completeWithApple(system: system, prompt: prompt)
        case .anthropic, .openai, .openrouter, .local:
            return try await completeInCloud(system: system, prompt: prompt)
        }
    }

    private func completeWithApple(system: String, prompt: String) async throws -> String {
        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            guard case .available = SystemLanguageModel.default.availability else {
                throw AIError.notConfigured(Self.appleModelStatus)
            }
            let session = LanguageModelSession(instructions: system)
            return try await session.respond(to: prompt).content
        }
        #endif
        throw AIError.notConfigured(Self.appleModelStatus)
    }

    private func completeInCloud(system: String, prompt: String) async throws -> String {
        let key = KeychainStore.key(for: provider)
        if provider.needsKey, key == nil {
            throw AIError.notConfigured("Add your \(provider.name) API key in Settings › AI.")
        }
        let model = Self.model(for: provider)
        guard !model.isEmpty else {
            throw AIError.notConfigured("Choose a model for \(provider.name) in Settings › AI.")
        }
        var request: URLRequest
        let body: [String: Any]
        if provider == .anthropic {
            request = URLRequest(url: URL(string: "https://api.anthropic.com/v1/messages")!)
            request.setValue(key ?? "", forHTTPHeaderField: "x-api-key")
            request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
            body = [
                "model": model, "max_tokens": 16_000, "system": system,
                "messages": [["role": "user", "content": prompt]],
            ]
        } else {
            request = URLRequest(url: try chatCompletionsURL())
            if let key { request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }
            body = [
                "model": model,
                "messages": [["role": "system", "content": system], ["role": "user", "content": prompt]],
            ]
        }
        request.httpMethod = "POST"
        request.timeoutInterval = provider == .local ? 600 : 180
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let json = try await APICall.send(request, provider: provider)
        let text: String?
        if provider == .anthropic {
            text = (json?["content"] as? [[String: Any]])?
                .compactMap { $0["type"] as? String == "text" ? $0["text"] as? String : nil }
                .joined()
        } else {
            text = ((json?["choices"] as? [[String: Any]])?.first?["message"] as? [String: Any])?["content"] as? String
        }
        guard let text, !text.isEmpty else { throw AIError.failed("\(provider.name) returned no text.") }
        return text
    }

    private func chatCompletionsURL() throws -> URL {
        switch provider {
        case .openai: return URL(string: "https://api.openai.com/v1/chat/completions")!
        case .openrouter: return URL(string: "https://openrouter.ai/api/v1/chat/completions")!
        default:
            guard let base = LocalModel.baseURL else {
                throw AIError.notConfigured("Enter your local model server's address in Settings › AI.")
            }
            return base.appendingPathComponent("chat/completions")
        }
    }

    // MARK: Conflict prompts

    private func versions(_ texts: ConflictTexts, limit: Int? = nil) -> String {
        let limit = limit ?? provider.maxCharsPerVersion
        func cut(_ text: String?) -> String {
            guard let text else { return "(this version was deleted or has no readable text)" }
            return text.count > limit ? String(text.prefix(limit)) + "\n[… shortened]" : text
        }
        var info = "File: \(texts.file)\n"
        if let local = texts.analysis.localInfo?.author { info += "Laptop version last saved by: \(local)\n" }
        if let cloud = texts.analysis.onedriveInfo?.author { info += "OneDrive version last saved by: \(cloud)\n" }
        info += "Automatic check: \(texts.analysis.reason)\n"
        return """
        \(info)
        === LAPTOP VERSION ===
        \(cut(texts.localText))
        === END LAPTOP VERSION ===

        === ONEDRIVE VERSION ===
        \(cut(texts.onedriveText))
        === END ONEDRIVE VERSION ===
        """
    }

    /// The user's own instructions, added to every conflict prompt.
    private var userInstructions: String {
        let text = AITrust.currentInstructions.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? "" : "\n\nThe user's instructions, which take priority:\n\(text)"
    }

    /// Which version to keep, why, and what each side changed. Uses Jev when it is on,
    /// unless useJev is false (to have the provider explain a Jev decision).
    func decide(
        _ texts: ConflictTexts, local: FileVersion?, onedrive: FileVersion?, useJev: Bool = Jev.isOn
    ) async throws -> AIDecision {
        func when(_ version: FileVersion?) -> String {
            version.map { $0.date.formatted(date: .abbreviated, time: .shortened) } ?? "deleted"
        }
        let system = """
        You help a user resolve a file sync conflict: the same file was changed on their laptop \
        and in OneDrive. Compare the two versions and decide which one to keep. Answer with JSON \
        only, no code fences: {"keep": "local" | "onedrive" | "ask", "reason": "one sentence", \
        "changes": "what only the laptop version has, then what only the OneDrive version has, \
        at most 80 words"}. Use "ask" when both versions have their own important changes or when \
        you are not sure. Never invent content. Answer in English.\(userInstructions)
        """
        let header = "Laptop saved: \(when(local)). OneDrive saved: \(when(onedrive)).\n\n"
        if useJev {
            let instructions = AITrust.currentInstructions.trimmingCharacters(in: .whitespacesAndNewlines)
            return try await Jev.decide(
                state: header + versions(texts, limit: Jev.maxCharsPerVersion), instructions: instructions
            )
        }
        let prompt = header + versions(texts)
        return Self.parseDecision(try await complete(system: system, prompt: prompt, quick: true))
    }

    static func parseDecision(_ answer: String) -> AIDecision {
        guard let start = answer.firstIndex(of: "{"), let end = answer.lastIndex(of: "}"),
              let data = String(answer[start...end]).data(using: .utf8),
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return AIDecision(keep: "ask", reason: "The AI gave no clear answer.", changes: answer)
        }
        let keep = json["keep"] as? String ?? "ask"
        return AIDecision(
            keep: ["local", "onedrive"].contains(keep) ? keep : "ask",
            reason: json["reason"] as? String ?? "",
            changes: json["changes"] as? String ?? ""
        )
    }

    /// Turns the user's explanation of a wrong suggestion into one new instruction line.
    func learn(file: String, suggested: AIDecision, userChoice: String, feedback: String) async throws -> String {
        let system = """
        You maintain the instructions an assistant follows when it resolves file sync conflicts. \
        The assistant suggested the wrong version. From the user's explanation, write ONE short, \
        general instruction (at most 30 words) that would have led to the user's choice. Do not \
        repeat an existing instruction. Mention a folder only if the user did. Output only the \
        instruction line.
        """
        let prompt = """
        Existing instructions:
        \(AITrust.currentInstructions.isEmpty ? "(none)" : AITrust.currentInstructions)

        File: \(file)
        The assistant suggested: \(suggested.title) — \(suggested.reason)
        The user chose: \(userChoice)
        The user's explanation: \(feedback)
        """
        return try await complete(system: system, prompt: prompt)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\"' \n-•"))
    }

    /// A merged file that keeps the changes of both versions. The user reviews it before it is used.
    func merge(_ texts: ConflictTexts) async throws -> String {
        let system = """
        You merge two versions of the same text file that were edited separately. Keep every change \
        from both versions. If both changed the same passage in contradicting ways, keep both variants \
        one after the other. Keep the original format, language and line endings. Output only the \
        complete merged file content, with no explanation and no code fences.\(userInstructions)
        """
        let answer = try await complete(system: system, prompt: versions(texts))
        return Self.stripCodeFence(answer)
    }

    static func stripCodeFence(_ text: String) -> String {
        var lines = text.components(separatedBy: "\n")
        guard lines.first?.hasPrefix("```") == true, lines.last?.trimmingCharacters(in: .whitespaces) == "```" else {
            return text
        }
        lines.removeFirst()
        lines.removeLast()
        return lines.joined(separator: "\n") + "\n"
    }
}
