import Foundation

// MARK: - Models decoded from the engine's JSON output

struct PairInfo: Decodable, Hashable {
    let name: String
    let local: String
    let onedrive: String
    let variant: String?
    let direction: String?
    let conflicts: Int?
    let pendingDeletions: Int?
}

struct EngineStatus: Decodable {
    let running: Bool
    let lastSync: String?
    let lastResult: String
    let conflicts: Int
    let pendingDeletions: Int
    let pairs: [PairInfo]
    let logFile: String
    let summaryFile: String
    let syncIntervalMinutes: Int
}

struct FileVersion: Codable, Hashable {
    let modified: Int
    let size: Int

    var date: Date { Date(timeIntervalSince1970: TimeInterval(modified)) }
}

struct Conflict: Decodable, Identifiable, Hashable {
    let pair: String
    let file: String
    let relatedFile: String?
    let reason: String
    let local: FileVersion?
    let onedrive: FileVersion?
    let newer: String?
    let isText: Bool
    let diffPreview: String
    let analysis: ConflictAnalysis?

    var id: String { "\(pair)|\(file)" }
    var isNameConflict: Bool { relatedFile != nil }

    /// "local" or "onedrive" when the file was deleted on that side.
    var deletedSide: String? {
        if local == nil, onedrive != nil { return "local" }
        if onedrive == nil, local != nil { return "onedrive" }
        return nil
    }

    /// Button text for keeping one side. Keeping the deleted side deletes the file on the other.
    func keepTitle(_ keep: String) -> String {
        switch (keep, deletedSide) {
        case ("onedrive", "onedrive"): return "Delete Locally"
        case ("local", "onedrive"): return "Restore to OneDrive"
        case ("local", "local"): return "Delete on OneDrive"
        case ("onedrive", "local"): return "Restore Locally"
        case ("local", _): return "Keep Local"
        case ("onedrive", _): return "Keep OneDrive"
        case ("merged", _): return "Use Merge"
        default: return keep
        }
    }

    /// Short badge for the decision in the list.
    func keepBadge(_ keep: String) -> String {
        if keep == deletedSide { return "Delete" }
        if deletedSide != nil { return "Restore" }
        switch keep {
        case "local": return "Local"
        case "onedrive": return "OneDrive"
        case "merged": return "Merged"
        default: return keep
        }
    }
}

struct SaveInfo: Decodable, Hashable {
    let author: String?
    let revision: Int?
}

/// The engine's suggestion for a conflict (no AI involved).
struct ConflictAnalysis: Decodable, Hashable {
    /// "local", "onedrive", "merge" or nil.
    let suggestion: String?
    /// "high", "medium", "low" or nil.
    let confidence: String?
    let reason: String
    let mergeable: Bool
    let localInfo: SaveInfo?
    let onedriveInfo: SaveInfo?

    var keepSide: String? { suggestion == "local" || suggestion == "onedrive" ? suggestion : nil }
}

/// Both versions' text for AI help (`--conflict-texts`).
struct ConflictTexts: Decodable {
    let pair: String
    let file: String
    let localText: String?
    let onedriveText: String?
    let truncated: Bool
    let mergeable: Bool
    let analysis: ConflictAnalysis
}

struct DeletionItem: Codable, Hashable {
    let pair: String
    let action: String
    let file: String
}

struct DeletionGroup: Decodable, Identifiable, Hashable {
    let id: String
    let pair: String
    let action: String
    let remainingOn: String
    let path: String
    let label: String
    let recommended: Bool
    let count: Int
    let items: [DeletionItem]
}

/// A planned change from `--preview` (FreeFileSync's Compare).
struct PreviewAction: Decodable, Hashable {
    let pair: String
    let file: String
    let action: String
    let from: String?
    let held: Bool?
}

struct PreviewConflict: Decodable, Hashable {
    let pair: String
    let file: String
    let reason: String
}

struct SkippedPair: Decodable, Hashable {
    let pair: String
    let reason: String?
}

struct PreviewPayload: Decodable {
    let actions: [PreviewAction]
    let conflicts: [PreviewConflict]
    let skippedPairs: [SkippedPair]
    let error: String?
}

/// A per-file action chosen in the preview.
struct PreviewOverride: Encodable, Hashable {
    let pair: String
    let file: String
    let action: String
}

struct EngineNotification: Decodable {
    let title: String
    let message: String
    /// Window a click opens: "summary", "conflicts" or "deletions".
    let open: String?
}

struct ConflictDecision: Encodable {
    let pair: String
    let file: String
    /// "local", "onedrive" or "merged" (then `mergedText` is written to both sides).
    let keep: String
    var mergedText: String? = nil

    enum CodingKeys: String, CodingKey {
        case pair, file, keep
        case mergedText = "merged_text"
    }
}

struct BatchResult: Decodable {
    struct Failure: Decodable { let file: String; let error: String }
    let done: [String]?
    let failed: [FailureOrName]?
    let activated: [String]?
    let error: String?
    let errors: [String]?
}

/// `failed` is a list of names for deletions and of {file, error} for conflicts.
enum FailureOrName: Decodable {
    case name(String)
    case failure(file: String, error: String)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let name = try? container.decode(String.self) {
            self = .name(name)
        } else {
            let failure = try container.decode(BatchResult.Failure.self)
            self = .failure(file: failure.file, error: failure.error)
        }
    }

    var text: String {
        switch self {
        case .name(let name): return name
        case .failure(let file, let error): return "\(file): \(error)"
        }
    }
}

enum EngineError: LocalizedError {
    case pythonNotFound
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .pythonNotFound:
            return "The Python bundled with SyncBridge was not found. Reinstall SyncBridge, or set a Python path in Settings › Schedule & General."
        case .failed(let message):
            return message
        }
    }
}

// MARK: - Engine runner

/// Runs the bundled Python engine (`syncbridge.py`) and decodes its JSON output.
struct Engine {
    static let pythonDefaultsKey = "pythonPath"
    static let pythonCandidates = [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]

    var scriptURL: URL {
        Bundle.main.resourceURL!.appendingPathComponent("engine/syncbridge.py")
    }

    /// Python shipped inside the app (see scripts/build-app.sh).
    static var bundledPython: String? {
        Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3").path
    }

    /// A Python set in Settings wins; otherwise the bundled one, then a system Python.
    var pythonPath: String? {
        let configured = UserDefaults.standard.string(forKey: Self.pythonDefaultsKey) ?? ""
        let candidates = configured.isEmpty
            ? [Self.bundledPython].compactMap { $0 } + Self.pythonCandidates
            : [configured]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private var environment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        if let icon = Bundle.main.url(forResource: "AppIcon", withExtension: "icns") {
            env["SYNCBRIDGE_APP_ICON"] = icon.path
        }
        // The app posts notifications itself, so they show the SyncBridge icon.
        env["SYNCBRIDGE_APP_NOTIFICATIONS"] = "1"
        return env
    }

    /// Runs the engine and returns its exit code and standard output.
    func run(_ arguments: [String]) async throws -> (code: Int32, output: Data) {
        guard let python = pythonPath else { throw EngineError.pythonNotFound }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [scriptURL.path] + arguments
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice

        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    try process.run()
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    continuation.resume(returning: (process.terminationStatus, data))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    func decode<T: Decodable>(_ type: T.Type, _ arguments: [String]) async throws -> T {
        let (code, data) = try await run(arguments)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            let text = String(data: data, encoding: .utf8) ?? ""
            throw EngineError.failed("The engine returned no valid data (exit \(code)). \(text.prefix(300))")
        }
    }

    /// Writes a JSON payload to a temporary file for the duration of `body`.
    func withJSONFile<T: Encodable, R>(_ payload: T, _ body: (String) async throws -> R) async throws -> R {
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("syncbridge-\(UUID().uuidString).json")
        try JSONEncoder().encode(payload).write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        return try await body(file.path)
    }

    /// Runs a command that reads a JSON payload from a file.
    func runWithFile<T: Encodable>(_ flag: String, payload: T, extra: [String] = []) async throws -> BatchResult {
        try await withJSONFile(payload) { path in
            try await decode(BatchResult.self, [flag, path] + extra)
        }
    }

    /// Notifications the engine handed to the app (lines with a fixed prefix).
    static func notifications(in output: Data) -> [EngineNotification] {
        let prefix = "SYNCBRIDGE_NOTIFY "
        return (String(data: output, encoding: .utf8) ?? "")
            .split(whereSeparator: \.isNewline)
            .filter { $0.hasPrefix(prefix) }
            .compactMap { try? JSONDecoder().decode(EngineNotification.self, from: Data($0.dropFirst(prefix.count).utf8)) }
    }

    // MARK: Config (kept as a dictionary so unknown fields survive)

    func loadConfig() async throws -> [String: Any] {
        let (_, data) = try await run(["--get-config"])
        guard let config = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw EngineError.failed("Could not read the configuration.")
        }
        return config
    }

    func saveConfig(_ config: [String: Any]) async throws -> [String] {
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("syncbridge-config-\(UUID().uuidString).json")
        try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted]).write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        let result = try await decode(BatchResult.self, ["--set-config", file.path])
        return result.errors ?? []
    }
}
