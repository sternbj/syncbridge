import Foundation

/// Editable copy of the config. Unknown fields stay in `raw` (and per pair in
/// `Pair.raw`) and are written back unchanged.
struct ConfigDraft {
    struct Pair: Identifiable {
        static let categories = [
            "local_new", "local_changed", "local_deleted",
            "onedrive_new", "onedrive_changed", "onedrive_deleted",
        ]
        static let twoWay = Dictionary(uniqueKeysWithValues: categories.map {
            ($0, $0.hasPrefix("local_") ? "to_onedrive" : "to_local")
        })

        let id = UUID()
        var raw: [String: Any] = [:]
        var name: String
        var local: String
        var onedrive: String
        var enabled = true
        var variant = "two-way"
        var direction = "to_onedrive"
        var directions = Pair.twoWay
        var include = ""
        var exclude = ""
        var minSizeMB = 0.0
        var maxSizeMB = 0.0
        var maxAgeDays = 0

        init(name: String, local: String, onedrive: String) {
            self.name = name
            self.local = local
            self.onedrive = onedrive
        }

        init(raw: [String: Any]) {
            self.raw = raw
            name = raw["name"] as? String ?? ""
            local = raw["local"] as? String ?? ""
            onedrive = raw["onedrive"] as? String ?? ""
            enabled = raw["enabled"] as? Bool ?? true
            variant = raw["variant"] as? String ?? "two-way"
            direction = raw["direction"] as? String ?? "to_onedrive"
            directions = raw["directions"] as? [String: String] ?? Pair.twoWay
            let filter = raw["filter"] as? [String: Any] ?? [:]
            include = ConfigDraft.lines(filter["include"])
            exclude = ConfigDraft.lines(filter["exclude"])
            minSizeMB = Double(filter["min_size_bytes"] as? Int ?? 0) / 1_048_576
            maxSizeMB = Double(filter["max_size_bytes"] as? Int ?? 0) / 1_048_576
            maxAgeDays = filter["max_age_days"] as? Int ?? 0
        }

        var payload: [String: Any] {
            var entry = raw
            entry["name"] = name
            entry["local"] = local
            entry["onedrive"] = onedrive
            entry["enabled"] = enabled ? nil : false
            entry["variant"] = variant == "two-way" ? nil : variant
            entry["direction"] = ["mirror", "update"].contains(variant) ? direction : nil
            entry["directions"] = variant == "custom" ? directions : nil
            var filter: [String: Any] = [:]
            if !ConfigDraft.list(include).isEmpty { filter["include"] = ConfigDraft.list(include) }
            if !ConfigDraft.list(exclude).isEmpty { filter["exclude"] = ConfigDraft.list(exclude) }
            if minSizeMB > 0 { filter["min_size_bytes"] = Int(minSizeMB * 1_048_576) }
            if maxSizeMB > 0 { filter["max_size_bytes"] = Int(maxSizeMB * 1_048_576) }
            if maxAgeDays > 0 { filter["max_age_days"] = maxAgeDays }
            entry["filter"] = filter.isEmpty ? nil : filter
            return entry
        }

        var hasFilter: Bool {
            !ConfigDraft.list(include).isEmpty || !ConfigDraft.list(exclude).isEmpty
                || minSizeMB > 0 || maxSizeMB > 0 || maxAgeDays > 0
        }
    }

    var raw: [String: Any] = [:]
    var pairs: [Pair] = []
    var conflictRule = "ask"
    var autoDeleteLimit = 50
    var trashRetentionDays = 30
    var syncIntervalMinutes = 60
    var deletionHandling = "versioning"
    var versioningStyle = "timestamp_folder"
    var versioningFolder = ""
    var versioningMaxCount = 0
    var versioningMinCount = 0
    var runAfterSync = ""
    var excludeDirs = ""
    var excludePatterns = ""
    var excludeNames = ""
    var folderRules: [FolderRule] = []
    /// One name per line, highest rank first.
    var authorPriority = ""

    /// A fixed rule for a folder inside the pairs, e.g. "Clients/**" → protect.
    struct FolderRule: Identifiable, Equatable {
        let id = UUID()
        var pattern: String
        var rule: String

        static let choices: [(rule: String, label: String)] = [
            ("protect", "Never delete (restore deleted files)"),
            ("keep_local", "Conflicts: keep the local version"),
            ("keep_onedrive", "Conflicts: keep the cloud version"),
            ("keep_newer", "Conflicts: keep the newer version"),
            ("keep_older", "Conflicts: keep the older version"),
        ]
    }

    init() {}

    init(raw: [String: Any]) {
        self.raw = raw
        pairs = (raw["pairs"] as? [[String: Any]] ?? []).map(Pair.init(raw:))
        conflictRule = raw["conflict_rule"] as? String ?? "ask"
        autoDeleteLimit = raw["auto_delete_limit"] as? Int ?? 50
        trashRetentionDays = raw["trash_retention_days"] as? Int ?? 30
        syncIntervalMinutes = raw["sync_interval_minutes"] as? Int ?? 60
        deletionHandling = raw["deletion_handling"] as? String ?? "versioning"
        versioningStyle = raw["versioning_style"] as? String ?? "timestamp_folder"
        versioningFolder = raw["trash_dir"] as? String ?? ""
        versioningMaxCount = raw["versioning_max_count"] as? Int ?? 0
        versioningMinCount = raw["versioning_min_count"] as? Int ?? 0
        runAfterSync = raw["run_after_sync"] as? String ?? ""
        excludeDirs = Self.lines(raw["exclude_dirs"])
        excludePatterns = Self.lines(raw["exclude_path_patterns"])
        excludeNames = Self.lines(raw["exclude_names"])
        folderRules = (raw["folder_rules"] as? [[String: Any]] ?? []).map {
            FolderRule(pattern: $0["pattern"] as? String ?? "", rule: $0["rule"] as? String ?? "protect")
        }
        authorPriority = Self.lines(raw["author_priority"])
    }

    var payload: [String: Any] {
        var result = raw
        result["pairs"] = pairs.map(\.payload)
        result["conflict_rule"] = conflictRule
        result["auto_delete_limit"] = autoDeleteLimit
        result["trash_retention_days"] = trashRetentionDays
        result["sync_interval_minutes"] = syncIntervalMinutes
        result["deletion_handling"] = deletionHandling
        result["versioning_style"] = versioningStyle
        result["trash_dir"] = versioningFolder.isEmpty ? nil : versioningFolder
        result["versioning_max_count"] = versioningMaxCount
        result["versioning_min_count"] = versioningMinCount
        result["run_after_sync"] = runAfterSync.isEmpty ? nil : runAfterSync
        result["exclude_dirs"] = Self.list(excludeDirs)
        result["exclude_path_patterns"] = Self.list(excludePatterns)
        result["exclude_names"] = Self.list(excludeNames)
        result["folder_rules"] = folderRules.map { ["pattern": $0.pattern, "rule": $0.rule] }
        result["author_priority"] = Self.list(authorPriority)
        return result
    }

    static func lines(_ value: Any?) -> String {
        (value as? [String] ?? []).joined(separator: "\n")
    }

    static func list(_ text: String) -> [String] {
        text.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }
}

/// Cloud folders that macOS File Provider apps (OneDrive, iCloud, Dropbox,
/// Google Drive …) keep in ~/Library/CloudStorage.
enum CloudAccounts {
    static var root: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/CloudStorage")
    }

    static var all: [URL] {
        (try? FileManager.default.contentsOfDirectory(
            at: root, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
        ))?.sorted { $0.path < $1.path } ?? []
    }

    /// "OneDrive-Contoso" → "OneDrive (Contoso)"
    static func displayName(_ account: URL) -> String {
        let parts = account.lastPathComponent.split(separator: "-", maxSplits: 1).map(String.init)
        return parts.count == 2 ? "\(parts[0]) (\(parts[1]))" : parts[0]
    }

    /// Shows a cloud or drive path as "Account › sub/folder" or "Drive › sub/folder".
    static func display(_ path: String) -> String {
        let full = (path as NSString).expandingTildeInPath
        for account in all where full == account.path || full.hasPrefix(account.path + "/") {
            return joined(displayName(account), String(full.dropFirst(account.path.count)))
        }
        if let drive = ExternalDrives.name(of: full) {
            return joined("Drive \(drive)", String(full.dropFirst("/Volumes/\(drive)".count)))
        }
        return path
    }

    private static func joined(_ head: String, _ rest: String) -> String {
        let rest = rest.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return rest.isEmpty ? head : "\(head) › \(rest)"
    }
}

/// USB sticks and external disks, which macOS mounts under /Volumes.
enum ExternalDrives {
    static let root = URL(fileURLWithPath: "/Volumes")

    /// The drive name for a path on an external drive, nil for anything else.
    static func name(of path: String) -> String? {
        let parts = (path as NSString).expandingTildeInPath.split(separator: "/", omittingEmptySubsequences: true)
        return parts.count >= 2 && parts[0] == "Volumes" ? String(parts[1]) : nil
    }

    /// True when the path is on a drive that is not plugged in. Creating folders
    /// there would put them on the Mac's own disk, so the app never does.
    static func isDisconnected(_ path: String) -> Bool {
        guard let drive = name(of: path) else { return false }
        return !FileManager.default.fileExists(atPath: "/Volumes/\(drive)")
    }

    static func contains(_ volume: URL, _ path: String) -> Bool {
        let full = (path as NSString).expandingTildeInPath
        return full == volume.path || full.hasPrefix(volume.path + "/")
    }
}
