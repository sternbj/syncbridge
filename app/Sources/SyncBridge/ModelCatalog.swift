import Foundation

/// A model a provider offers, as listed by its /models endpoint.
struct CatalogModel: Codable, Equatable, Hashable {
    let id: String
    let created: Date
    /// Context window in tokens; 0 when the provider doesn't say.
    let context: Int

    /// A name like "~moonshotai/kimi-latest" that always points to the newest model of its family.
    var isAlwaysNewest: Bool { id.hasPrefix("~") || id.hasSuffix("-latest") }

    /// "moonshotai/kimi-k3" → "moonshotai"; "" for providers without makers in their names.
    var maker: String { id.contains("/") ? String(id.prefix { $0 != "/" }).replacingOccurrences(of: "~", with: "") : "" }
}

/// A newer version of the model a provider is set to.
struct ModelUpgrade: Equatable {
    let provider: AIProvider
    let current: String
    let newer: String
}

/// Once a week the app downloads the providers' model lists and keeps only what is worth choosing:
/// text models from the last 12 months, and of those only the newest version of each family.
/// No AI is involved: families come from the model names (maker + name without version numbers,
/// dates, sizes or words like "preview").
enum ModelCatalog {
    static let checkInterval: TimeInterval = 7 * 86_400
    static let maxAge: TimeInterval = 365 * 86_400
    static let minContext = 32_000
    static let providers: [AIProvider] = [.openrouter, .anthropic, .openai]
    private static let lastCheckKey = "modelCatalog.lastCheck"
    private static func listKey(_ provider: AIProvider) -> String { "modelCatalog.\(provider.rawValue)" }
    private static func notifiedKey(_ provider: AIProvider) -> String { "modelCatalog.notified.\(provider.rawValue)" }

    // MARK: Rules

    /// Words that describe a release, not a different model.
    private static let releaseWords: Set<String> = ["preview", "exp", "instruct", "it", "latest"]

    /// The family a model belongs to; versions of the same model share it.
    static func family(of id: String) -> String {
        let id = id.lowercased().split(separator: ":").first.map(String.init) ?? ""
        let maker = id.contains("/") ? String(id.prefix { $0 != "/" }) + "/" : ""
        let name = id.dropFirst(maker.count)
        var parts: [String] = []
        for token in name.split(separator: "-").map(String.init) {
            if isSize(token) || releaseWords.contains(token) { continue }
            if let prefix = versionPrefix(token) {
                // "3-5", "4-0731" or "2024-08-06" right after a version are part of that version.
                if prefix.isEmpty, parts.last?.hasSuffix("#") == true { continue }
                parts.append(prefix + "#")
            } else {
                parts.append(token)
            }
        }
        while parts.last == "#" { parts.removeLast() }
        return maker + parts.joined(separator: "-")
    }

    /// "3.1" → "", "k2.5" → "k", "qwen3" → "qwen", "v4" → "v"; nil when the token has no version.
    private static func versionPrefix(_ token: String) -> String? {
        let letters = token.prefix { $0.isLetter }
        let rest = token.dropFirst(letters.count)
        guard let first = rest.first, first.isNumber,
              rest.allSatisfy({ $0.isNumber || $0 == "." }), rest.last != "." else { return nil }
        return String(letters)
    }

    /// Sizes like "70b", "a22b", "1.7b" or "8x22b".
    private static func isSize(_ token: String) -> Bool {
        token.range(of: #"^(a?\d+(\.\d+)?|\d+x\d+)[bt]$"#, options: .regularExpression) != nil
    }

    /// Models from the last 12 months, only the newest of each family, sorted by name.
    static func newestPerFamily(_ models: [CatalogModel], now: Date) -> [CatalogModel] {
        var newest: [String: CatalogModel] = [:]
        for model in models where now.timeIntervalSince(model.created) <= maxAge {
            let key = family(of: model.id)
            if let kept = newest[key], kept.created >= model.created { continue }
            newest[key] = model
        }
        return newest.values.sorted { $0.id < $1.id }
    }

    /// The newer model of the same family, if the list has one. Unknown names and
    /// "always newest" names are left alone.
    static func replacement(for id: String, in list: [CatalogModel]) -> String? {
        let current = CatalogModel(id: id, created: .distantPast, context: 0)
        guard !id.isEmpty, !current.isAlwaysNewest else { return nil }
        let key = family(of: id)
        guard let newest = list.filter({ !$0.isAlwaysNewest && family(of: $0.id) == key })
            .max(by: { $0.created < $1.created }), newest.id != id else { return nil }
        return newest.id
    }

    static func isDue(lastCheck: Date?, now: Date) -> Bool {
        guard let lastCheck else { return true }
        return now.timeIntervalSince(lastCheck) >= checkInterval
    }

    // MARK: Reading the providers' lists

    /// OpenRouter: text-only chat models; no :batch or :free copies, no routers, no tiny contexts.
    static func parseOpenRouter(_ data: Data) -> [CatalogModel] {
        entries(data).compactMap { entry in
            guard let id = entry["id"] as? String, !id.contains(":"), !id.hasPrefix("openrouter/"),
                  let created = entry["created"] as? Double,
                  (entry["architecture"] as? [String: Any])?["output_modalities"] as? [String] == ["text"],
                  let context = entry["context_length"] as? Int, context >= minContext else { return nil }
            return CatalogModel(id: id, created: Date(timeIntervalSince1970: created), context: context)
        }
    }

    /// Anthropic: every listed model is a chat model.
    static func parseAnthropic(_ data: Data) -> [CatalogModel] {
        let dates = ISO8601DateFormatter()
        dates.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plainDates = ISO8601DateFormatter()
        return entries(data).compactMap { entry in
            guard let id = entry["id"] as? String, let text = entry["created_at"] as? String,
                  let created = dates.date(from: text) ?? plainDates.date(from: text) else { return nil }
            return CatalogModel(id: id, created: created, context: 0)
        }
    }

    /// OpenAI lists everything (speech, images, embeddings); keep the chat models.
    static func parseOpenAI(_ data: Data) -> [CatalogModel] {
        let excluded = ["audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "moderation"]
        return entries(data).compactMap { entry in
            guard let id = entry["id"] as? String, let created = entry["created"] as? Double,
                  id.hasPrefix("gpt-") || id.range(of: #"^o\d"#, options: .regularExpression) != nil,
                  !excluded.contains(where: id.contains) else { return nil }
            return CatalogModel(id: id, created: Date(timeIntervalSince1970: created), context: 0)
        }
    }

    private static func entries(_ data: Data) -> [[String: Any]] {
        ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["data"] as? [[String: Any]] ?? []
    }

    // MARK: Weekly check

    static var lastCheck: Date? { UserDefaults.standard.object(forKey: lastCheckKey) as? Date }

    /// The saved list for a provider; empty before the first check.
    static func models(for provider: AIProvider) -> [CatalogModel] {
        guard let data = UserDefaults.standard.data(forKey: listKey(provider)) else { return [] }
        return (try? JSONDecoder().decode([CatalogModel].self, from: data)) ?? []
    }

    /// A newer version of the model the provider is set to.
    static func upgrade(for provider: AIProvider) -> ModelUpgrade? {
        let current = AIAssistant.model(for: provider)
        return replacement(for: current, in: models(for: provider))
            .map { ModelUpgrade(provider: provider, current: current, newer: $0) }
    }

    /// Downloads the lists when a week has passed (or always, with force). Returns upgrades
    /// the user hasn't been told about yet.
    @discardableResult
    static func refresh(force: Bool = false, now: Date = Date()) async -> [ModelUpgrade] {
        guard force || isDue(lastCheck: lastCheck, now: now) else { return [] }
        var anyLoaded = false
        for provider in providers {
            guard let list = await download(provider), !list.isEmpty else { continue }
            let kept = newestPerFamily(list, now: now)
            UserDefaults.standard.set(try? JSONEncoder().encode(kept), forKey: listKey(provider))
            anyLoaded = true
        }
        // Without a connection, try again at the next check instead of waiting a week.
        if anyLoaded { UserDefaults.standard.set(now, forKey: lastCheckKey) }
        return providers.compactMap(upgrade).filter { upgrade in
            let key = notifiedKey(upgrade.provider)
            guard UserDefaults.standard.string(forKey: key) != upgrade.newer else { return false }
            UserDefaults.standard.set(upgrade.newer, forKey: key)
            return true
        }
    }

    /// Switches the provider to the newer model the user accepted.
    static func accept(_ upgrade: ModelUpgrade) {
        UserDefaults.standard.set(upgrade.newer, forKey: AIAssistant.modelKeyPrefix + upgrade.provider.rawValue)
    }

    /// OpenRouter's list is public; Anthropic and OpenAI need the saved key.
    private static func download(_ provider: AIProvider) async -> [CatalogModel]? {
        var request: URLRequest
        switch provider {
        case .openrouter:
            request = URLRequest(url: URL(string: "https://openrouter.ai/api/v1/models")!)
        case .anthropic:
            guard let key = KeychainStore.key(for: provider) else { return nil }
            request = URLRequest(url: URL(string: "https://api.anthropic.com/v1/models?limit=1000")!)
            request.setValue(key, forHTTPHeaderField: "x-api-key")
            request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        case .openai:
            guard let key = KeychainStore.key(for: provider) else { return nil }
            request = URLRequest(url: URL(string: "https://api.openai.com/v1/models")!)
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        default:
            return nil
        }
        request.timeoutInterval = 30
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        switch provider {
        case .anthropic: return parseAnthropic(data)
        case .openai: return parseOpenAI(data)
        default: return parseOpenRouter(data)
        }
    }
}
