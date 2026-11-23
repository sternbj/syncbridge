import CryptoKit
import Foundation

/// What the AI would do with one conflict.
struct AIDecision: Codable, Hashable {
    /// "local", "onedrive" or "ask" (the AI is not sure).
    var keep: String
    var reason: String
    /// What only each side has, in a few words.
    var changes: String = ""

    var title: String {
        switch keep {
        case "local": return "Keep Local"
        case "onedrive": return "Keep OneDrive"
        default: return "Ask me"
        }
    }
}

/// An AI answer kept on disk, valid while both file versions and the instructions stay the same.
struct SavedSuggestion: Codable {
    var decision: AIDecision
    var local: FileVersion?
    var onedrive: FileVersion?
    var instructions: String

    func fits(_ conflict: Conflict) -> Bool {
        local == conflict.local && onedrive == conflict.onedrive
            && instructions == AITrust.currentFingerprint
    }
}

/// One conflict where the user decided after seeing the AI's suggestion.
struct AIRecord: Codable {
    var date: Date
    var file: String
    var ai: String
    var user: String
    /// Which instructions the AI had; changing them starts the count again.
    var instructions: String

    var agreed: Bool { ai == user }
}

/// The user's instructions for the AI, the record of how often the AI agreed
/// with the user, and the switch for automatic decisions.
///
/// Automatic decisions are only offered after the AI agreed with every one of
/// at least `minimumDecisions` decisions in the last `windowDays`, all made
/// with the current instructions. One disagreement switches them off again.
@MainActor
final class AITrust: ObservableObject {
    static let shared = AITrust()
    static let windowDays = 30
    static let minimumDecisions = 20
    nonisolated static let instructionsKey = "aiInstructions"
    private static let autoKey = "aiAutoDecide"

    @Published private(set) var records: [AIRecord] = []
    @Published var instructions: String {
        didSet {
            guard instructions != oldValue else { return }
            UserDefaults.standard.set(instructions, forKey: Self.instructionsKey)
            autoDecide = false
        }
    }
    @Published var autoDecide: Bool {
        didSet { UserDefaults.standard.set(autoDecide && isEligible, forKey: Self.autoKey) }
    }

    private init() {
        instructions = UserDefaults.standard.string(forKey: Self.instructionsKey) ?? ""
        autoDecide = UserDefaults.standard.bool(forKey: Self.autoKey)
        records = (try? JSONDecoder().decode([AIRecord].self, from: Data(contentsOf: Self.fileURL))) ?? []
        if !isEligible { autoDecide = false }
    }

    /// Instructions as the AI sees them; read from any thread.
    nonisolated static var currentInstructions: String {
        UserDefaults.standard.string(forKey: instructionsKey) ?? ""
    }

    /// The instructions plus who decides (Jev or the provider, with its model): switching either
    /// starts the count again, and saved answers from another decider no longer fit.
    nonisolated static var currentFingerprint: String {
        fingerprint(currentInstructions + "\n#decider:" + AIAssistant.deciderID)
    }

    nonisolated static func fingerprint(_ text: String) -> String {
        SHA256.hash(data: Data(text.trimmingCharacters(in: .whitespacesAndNewlines).utf8))
            .prefix(8).map { String(format: "%02x", $0) }.joined()
    }

    private static var fileURL: URL { supportFile("ai-decisions.json") }

    /// A file in ~/Library/Application Support/SyncBridge.
    nonisolated static func supportFile(_ name: String) -> URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SyncBridge/\(name)")
    }

    /// Writes JSON next to the app's other files; failures only cost a cache or a statistic.
    nonisolated static func save<T: Encodable>(_ value: T, to url: URL) {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? JSONEncoder().encode(value).write(to: url, options: .atomic)
    }

    /// Records that count: the last 30 days, made with the current instructions.
    var window: [AIRecord] {
        let since = Date().addingTimeInterval(-Double(Self.windowDays) * 86_400)
        let current = Self.currentFingerprint
        return records.filter { $0.date >= since && $0.instructions == current }
    }

    var agreedCount: Int { window.filter(\.agreed).count }

    /// "18 of 20 (90%)"
    static func rate(_ records: [AIRecord]) -> String {
        let agreed = records.filter(\.agreed).count
        let percent = records.isEmpty ? 0 : Int((Double(agreed) / Double(records.count) * 100).rounded(.down))
        return "\(agreed) of \(records.count) (\(percent)%)"
    }

    /// The question asked before automatic decisions are switched on.
    var confirmationText: String {
        """
        In total, the AI agreed with \(Self.rate(records)) of your decisions.
        In the last \(Self.windowDays) days, with your current instructions: \(Self.rate(window)).

        The AI will then resolve conflicts after each sync without asking. Deletions, name \
        conflicts and merges still wait for you, and overwritten versions are kept. Switch it \
        off any time here.
        """
    }

    var isEligible: Bool {
        window.count >= Self.minimumDecisions && agreedCount == window.count
    }

    var summary: String {
        let total = window.count
        if total == 0 {
            return "No decisions yet with the current instructions."
        }
        if agreedCount < total {
            return "The AI agreed with you \(agreedCount) of \(total) times in the last \(Self.windowDays) days. "
                + "Automatic decisions need every decision to match."
        }
        if total < Self.minimumDecisions {
            return "The AI agreed with all \(total) of your decisions so far. "
                + "\(Self.minimumDecisions - total) more matching decisions unlock automatic decisions."
        }
        return "The AI agreed with all \(total) of your decisions in the last \(Self.windowDays) days."
    }

    /// Saves the user's decisions next to the AI's suggestions. Returns the disagreements.
    @discardableResult
    func record(_ outcomes: [(file: String, ai: String, user: String)]) -> [(file: String, ai: String, user: String)] {
        let current = Self.currentFingerprint
        let new = outcomes.filter { $0.ai != "ask" }.map {
            AIRecord(date: Date(), file: $0.file, ai: $0.ai, user: $0.user, instructions: current)
        }
        guard !new.isEmpty else { return [] }
        records = Array((records + new).suffix(5_000))
        Self.save(records, to: Self.fileURL)
        let disagreements = new.filter { !$0.agreed }
        if !disagreements.isEmpty { autoDecide = false }
        return disagreements.map { ($0.file, $0.ai, $0.user) }
    }

    func addInstruction(_ line: String) {
        let line = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.isEmpty else { return }
        instructions = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
            .appending(instructions.isEmpty ? line : "\n" + line)
    }

    /// Content-based examples; folder and author rules belong in Settings › Conflicts.
    static let examples = [
        "If one version only changed formatting, spacing or a date, keep the other version.",
        "Keep the version that has more requirements or sections. Never drop a requirement.",
        "If one version is much shorter or looks half deleted, it is probably a mistake: keep the complete one.",
        "If both versions added different content, answer \"ask\" so I can merge them.",
        "Prefer the version with fewer placeholders like TODO, XXX or [insert].",
        "When you are not sure, answer \"ask\". Never guess.",
    ]
}
