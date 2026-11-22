import Foundation
import Testing
@testable import SyncBridge

/// The weekly model check: which models the picker shows and when a saved model is replaced.
@Suite struct ModelCatalogTests {
    static let now = Date(timeIntervalSince1970: 1_795_000_000) // Nov 2026
    static let day: TimeInterval = 86_400

    static func model(_ id: String, daysAgo: Double, context: Int = 200_000) -> CatalogModel {
        CatalogModel(id: id, created: now.addingTimeInterval(-daysAgo * day), context: context)
    }

    // MARK: Families: versions, dates, sizes and "preview" don't make a new family

    @Test(arguments: [
        ("moonshotai/kimi-k2", "moonshotai/kimi-k3"),
        ("moonshotai/kimi-k2-0905", "moonshotai/kimi-k2.6"),
        ("anthropic/claude-opus-4.1", "anthropic/claude-opus-5.5"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-5-5"),
        ("mistralai/mistral-small-2603", "mistralai/mistral-small-3.2-24b-instruct"),
        ("mistralai/mistral-medium-3-5", "mistralai/mistral-medium-3.1"),
        ("google/gemini-3.1-flash-lite-preview", "google/gemini-3.5-flash-lite"),
        ("google/gemma-3-27b-it", "google/gemma-4-31b-it"),
        ("qwen/qwen3-max", "qwen/qwen4-max"),
        ("deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4.1-flash"),
        ("openai/gpt-6-sol", "openai/gpt-6.2-sol"),
    ])
    func sameFamily(older: String, newer: String) {
        #expect(ModelCatalog.family(of: older) == ModelCatalog.family(of: newer))
    }

    @Test(arguments: [
        ("moonshotai/kimi-k3", "moonshotai/kimi-k3.1-pro"),
        ("moonshotai/kimi-k3", "moonshotai/kimi-k2-thinking"),
        ("anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5"),
        ("openai/gpt-6.2-sol", "openai/gpt-6.2-sol-pro"),
        ("google/gemini-3.8-flash", "google/gemini-3.5-flash-lite"),
        ("moonshotai/kimi-k3", "anthropic/kimi-k3"),
    ])
    func differentFamily(_ a: String, _ b: String) {
        #expect(ModelCatalog.family(of: a) != ModelCatalog.family(of: b))
    }

    // MARK: Only the newest version of each family

    @Test func keepsOnlyNewestVersion() {
        let models = [
            Self.model("moonshotai/kimi-k2", daysAgo: 500),
            Self.model("moonshotai/kimi-k2.5", daysAgo: 300),
            Self.model("moonshotai/kimi-k3", daysAgo: 120),
            Self.model("moonshotai/kimi-k3.1-pro", daysAgo: 30),
            Self.model("moonshotai/kimi-k2.7-code", daysAgo: 150),
        ]
        let ids = ModelCatalog.newestPerFamily(models, now: Self.now).map(\.id)
        #expect(ids == ["moonshotai/kimi-k2.7-code", "moonshotai/kimi-k3", "moonshotai/kimi-k3.1-pro"])
    }

    @Test func dropsModelsOlderThanAYear() {
        let models = [
            Self.model("mistralai/mistral-nemo", daysAgo: 400),
            Self.model("mistralai/mistral-small-2603", daysAgo: 200),
        ]
        #expect(ModelCatalog.newestPerFamily(models, now: Self.now).map(\.id) == ["mistralai/mistral-small-2603"])
    }

    // MARK: Reading OpenRouter's list

    @Test func readsOnlyUsableOpenRouterModels() throws {
        let json = """
        {"data": [
          {"id": "anthropic/claude-sonnet-5.5", "created": 1790618686, "context_length": 1000000,
           "architecture": {"output_modalities": ["text"]}},
          {"id": "anthropic/claude-sonnet-5.5:batch", "created": 1790618686, "context_length": 1000000,
           "architecture": {"output_modalities": ["text"]}},
          {"id": "google/gemma-4-31b-it:free", "created": 1775148486, "context_length": 262144,
           "architecture": {"output_modalities": ["text"]}},
          {"id": "google/gemini-3-pro-image", "created": 1781754054, "context_length": 131072,
           "architecture": {"output_modalities": ["image", "text"]}},
          {"id": "openrouter/auto", "created": 1793885889, "context_length": 2000000,
           "architecture": {"output_modalities": ["text"]}},
          {"id": "gryphe/mythomax-l2-13b", "created": 1688256000, "context_length": 8192,
           "architecture": {"output_modalities": ["text"]}},
          {"id": "~moonshotai/kimi-latest", "created": 1792778188, "context_length": 1048576,
           "architecture": {"output_modalities": ["text"]}}
        ]}
        """
        let ids = ModelCatalog.parseOpenRouter(Data(json.utf8)).map(\.id)
        #expect(ids == ["anthropic/claude-sonnet-5.5", "~moonshotai/kimi-latest"])
    }

    // MARK: Reading Anthropic's and OpenAI's lists (names exactly as their APIs accept them)

    @Test func readsAnthropicModels() {
        let json = """
        {"data": [
          {"type": "model", "id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5",
           "created_at": "2026-09-29T00:00:00Z"},
          {"type": "model", "id": "claude-opus-4-1-20250805", "display_name": "Claude Opus 4.1",
           "created_at": "2025-08-05T00:00:00.000Z"},
          {"type": "model", "id": "broken-entry"}
        ], "has_more": false}
        """
        let models = ModelCatalog.parseAnthropic(Data(json.utf8))
        #expect(models.map(\.id) == ["claude-sonnet-5-5", "claude-opus-4-1-20250805"])
        #expect(models.first?.created == ISO8601DateFormatter().date(from: "2026-09-29T00:00:00Z"))
        // Both dated and undated names land in one family, so the newer one replaces the older.
        #expect(ModelCatalog.replacement(for: "claude-opus-4-1-20250805",
                                         in: [Self.model("claude-opus-5-5", daysAgo: 60)]) == "claude-opus-5-5")
    }

    @Test func readsOnlyOpenAIChatModels() {
        let json = """
        {"object": "list", "data": [
          {"id": "gpt-6.2-sol", "object": "model", "created": 1794938371},
          {"id": "o4-mini", "object": "model", "created": 1744820942},
          {"id": "gpt-audio", "object": "model", "created": 1768862569},
          {"id": "gpt-realtime", "object": "model", "created": 1768862569},
          {"id": "gpt-5-image", "object": "model", "created": 1760447986},
          {"id": "gpt-4o-mini-tts", "object": "model", "created": 1742403959},
          {"id": "gpt-4o-transcribe", "object": "model", "created": 1742403959},
          {"id": "gpt-5-search-api", "object": "model", "created": 1759776663},
          {"id": "text-embedding-3-large", "object": "model", "created": 1705953180},
          {"id": "omni-moderation-latest", "object": "model", "created": 1731689265},
          {"id": "dall-e-3", "object": "model", "created": 1698785189},
          {"id": "whisper-1", "object": "model", "created": 1677532384}
        ]}
        """
        #expect(ModelCatalog.parseOpenAI(Data(json.utf8)).map(\.id) == ["gpt-6.2-sol", "o4-mini"])
    }

    @Test func latestAliasesAreMarked() {
        #expect(CatalogModel(id: "~moonshotai/kimi-latest", created: Self.now, context: 1).isAlwaysNewest)
        #expect(!CatalogModel(id: "moonshotai/kimi-k3", created: Self.now, context: 1).isAlwaysNewest)
    }

    // MARK: Replacing a saved model with its newer version

    @Test func replacesOutdatedModelWithNewestOfItsFamily() {
        let list = [Self.model("moonshotai/kimi-k3", daysAgo: 120), Self.model("anthropic/claude-opus-5.5", daysAgo: 60)]
        #expect(ModelCatalog.replacement(for: "moonshotai/kimi-k2.5", in: list) == "moonshotai/kimi-k3")
        #expect(ModelCatalog.replacement(for: "moonshotai/kimi-k3", in: list) == nil) // already the newest
        #expect(ModelCatalog.replacement(for: "my-own-finetune", in: list) == nil)    // unknown: left alone
        #expect(ModelCatalog.replacement(for: "~moonshotai/kimi-latest", in: list) == nil) // updates itself
    }

    // MARK: Once a week

    @Test func checksOncePerWeek() {
        #expect(ModelCatalog.isDue(lastCheck: nil, now: Self.now))
        #expect(!ModelCatalog.isDue(lastCheck: Self.now.addingTimeInterval(-6 * Self.day), now: Self.now))
        #expect(ModelCatalog.isDue(lastCheck: Self.now.addingTimeInterval(-8 * Self.day), now: Self.now))
    }
}
