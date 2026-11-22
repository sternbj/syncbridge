import AppKit
import Foundation
import UserNotifications

/// Shared app state: engine status, open conflicts, pending deletions and the sync schedule.
@MainActor
final class AppModel: ObservableObject {
    @Published var status: EngineStatus?
    @Published var conflicts: [Conflict] = []
    @Published var deletionGroups: [DeletionGroup] = []
    @Published var isSyncing = false
    @Published var isWorking = false
    @Published var message: String?
    @Published var preview: PreviewPayload?
    @Published var isComparing = false
    /// AI suggestions per conflict, shared by the Conflicts window and Settings › AI.
    @Published var aiSuggestions: [Conflict.ID: AIDecision] = [:]
    /// Progress of "Ask AI for All": how many conflicts are answered out of how many.
    @Published var aiProgress: (done: Int, total: Int)?
    /// The conflicts the AI is looking at right now.
    @Published var aiAsking: Set<Conflict.ID> = []
    /// Every AI answer, kept across restarts; aiSuggestions shows those still valid.
    private var savedSuggestions: [Conflict.ID: SavedSuggestion] =
        (try? JSONDecoder().decode([Conflict.ID: SavedSuggestion].self,
                                   from: Data(contentsOf: AITrust.supportFile("ai-suggestions.json")))) ?? [:]
    @Published private(set) var aiStopRequested = false
    private var aiQueue: [Conflict] = []
    private var aiFailure: String?
    var aiWorking: Bool { aiProgress != nil }

    static let realtimeKey = "realtimeSync"
    static let realtimeDelayKey = "realtimeDelaySeconds"

    let engine = Engine()
    private var scheduleTimer: Timer?
    private var statusTimer: Timer?
    private var modelCheckTimer: Timer?
    private var watcher: FolderWatcher?
    private var watchedPairs: [PairInfo] = []
    private var changedPairs: Set<String> = []
    private var realtimeTimer: Timer?
    private var lastSyncEnd = Date.distantPast
    private var wakeObserver: NSObjectProtocol?
    private var mountObserver: NSObjectProtocol?
    private let notificationDelegate = NotificationDelegate()

    init() {
        AIAssistant.migrate()
        UNUserNotificationCenter.current().delegate = notificationDelegate
        WindowActivation.shared.start()
        AppDelegate.onReopen = { [weak self] in self?.show("settings") }
        notificationDelegate.onOpen = { [weak self] target in
            Task { @MainActor in self?.show(target) }
        }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        Task {
            await refreshStatus()
            scheduleSyncs()
            // Opened by the user (not at login): show a window, since the menu bar icon is easy to miss.
            if !LoginItem.launchedInBackground {
                for _ in 0..<20 where windowOpener == nil {
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
                show("settings")
            }
            // First run shortly after launch, like the old login job.
            try? await Task.sleep(nanoseconds: 30 * 1_000_000_000)
            await syncNow()
        }
        statusTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { await self?.refreshStatus() }
        }
        // The model lists are downloaded once a week; looking every few hours catches a Mac that slept.
        Task { await checkModels() }
        modelCheckTimer = Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { [weak self] _ in
            Task { await self?.checkModels() }
        }
    }

    /// Tells the user when a newer version of the model they use is out; switching is their choice.
    private func checkModels() async {
        for upgrade in await ModelCatalog.refresh() where upgrade.provider == AIAssistant.provider {
            post(EngineNotification(
                title: "Newer AI model available",
                message: "\(upgrade.newer) is out (you use \(upgrade.current)). Switch in Settings › AI.",
                open: "ai"
            ))
        }
    }

    var intervalMinutes: Int { max(status?.syncIntervalMinutes ?? 60, 5) }

    static let intervalChoices: [(minutes: Int, label: String)] = [
        (15, "15 minutes"), (30, "30 minutes"), (60, "Hour"), (120, "2 hours"), (240, "4 hours"),
    ]

    /// Tab the Settings window shows ("folders", "conflicts", "deletions", "exclusions", "general").
    @Published var settingsTab = UserDefaults.standard.string(forKey: "settingsTab") ?? "folders" {
        didSet { UserDefaults.standard.set(settingsTab, forKey: "settingsTab") }
    }

    /// Changes how often the scheduled sync runs (saved in the config).
    func setInterval(_ minutes: Int) async {
        do {
            var config = try await engine.loadConfig()
            config["sync_interval_minutes"] = minutes
            let errors = try await engine.saveConfig(config)
            if !errors.isEmpty { message = errors.joined(separator: "\n") }
        } catch {
            message = error.localizedDescription
        }
        await refreshStatus()
    }

    /// The engine writes local time without a zone, e.g. "2026-11-17T04:03:48".
    private static let engineDateParser: DateFormatter = {
        let parser = DateFormatter()
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.timeZone = .current
        parser.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return parser
    }()

    var lastSyncDate: Date? { status?.lastSync.flatMap(Self.engineDateParser.date(from:)) }

    var lastSyncText: String {
        guard let raw = status?.lastSync else { return "Never synced" }
        guard let date = lastSyncDate else { return "Last sync: \(raw)" }
        let relative = RelativeDateTimeFormatter()
        relative.unitsStyle = .full
        return "Last sync \(relative.localizedString(for: date, relativeTo: Date()))"
    }

    /// Checks the wall clock every minute and after wake. A repeating timer of
    /// `interval` minutes does not count time the Mac sleeps, so on a laptop that
    /// sleeps often the next sync could be many hours late.
    func scheduleSyncs() {
        scheduleTimer?.invalidate()
        scheduleTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { await self?.syncIfDue() }
        }
        if wakeObserver == nil {
            wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in
                    // Give the network and OneDrive a moment after wake.
                    try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
                    await self?.syncIfDue()
                }
            }
        }
        if mountObserver == nil {
            mountObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didMountNotification, object: nil, queue: .main
            ) { [weak self] note in
                guard let volume = note.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL else { return }
                Task { @MainActor in await self?.driveConnected(volume) }
            }
        }
    }

    /// A USB stick or drive was plugged in: sync the pairs that use it.
    private func driveConnected(_ volume: URL) async {
        let pairs = (status?.pairs ?? [])
            .filter { ExternalDrives.contains(volume, $0.local) || ExternalDrives.contains(volume, $0.onedrive) }
            .map(\.name)
        guard !pairs.isEmpty else { return }
        // Give Spotlight and the file system a moment after mounting.
        try? await Task.sleep(nanoseconds: 5 * 1_000_000_000)
        await syncNow(pairs: pairs)
    }

    func syncIfDue() async {
        let last = max(lastSyncDate ?? .distantPast, lastSyncEnd)
        guard !isSyncing, Date().timeIntervalSince(last) >= TimeInterval(intervalMinutes * 60) else { return }
        await syncNow()
    }

    // MARK: Engine calls

    func refreshStatus() async {
        do {
            status = try await engine.decode(EngineStatus.self, ["--status", "--json"])
            if status?.running == true && !isSyncing { isSyncing = true } else if status?.running == false { isSyncing = false }
            updateWatcher()
        } catch {
            message = error.localizedDescription
        }
    }

    /// Syncs all pairs, or only `pairs`; `overrides` are per-file choices from the preview.
    func syncNow(pairs: [String]? = nil, overrides: [PreviewOverride] = []) async {
        guard !isSyncing else { return }
        isSyncing = true
        defer {
            isSyncing = false
            lastSyncEnd = Date()
        }
        var arguments = ["--auto-delete"]
        if let pairs { arguments += ["--pairs"] + pairs }
        do {
            let (_, output) = overrides.isEmpty
                ? try await engine.run(arguments)
                : try await engine.withJSONFile(overrides) { path in
                    try await engine.run(arguments + ["--overrides", path])
                }
            Engine.notifications(in: output).forEach(post)
            StarPrompt.syncSucceeded()
        } catch {
            message = error.localizedDescription
        }
        await refreshStatus()
        if !conflicts.isEmpty || AITrust.shared.autoDecide { await loadConflicts() }
        await decideAutomatically()
    }

    /// Plans a sync without changing anything (FreeFileSync's Compare).
    func loadPreview() async {
        isComparing = true
        defer { isComparing = false }
        do {
            preview = try await engine.decode(PreviewPayload.self, ["--preview"])
            if let error = preview?.error { message = error }
        } catch {
            message = error.localizedDescription
        }
    }

    private func post(_ notification: EngineNotification) {
        let content = UNMutableNotificationContent()
        content.title = notification.title
        content.body = notification.message
        content.sound = .default
        content.userInfo = ["open": notification.open ?? "summary"]
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    // MARK: Real-time sync

    /// Watches all pair folders; a change syncs that pair after a quiet period.
    func updateWatcher() {
        let pairs = UserDefaults.standard.bool(forKey: Self.realtimeKey) ? (status?.pairs ?? []) : []
        guard pairs != watchedPairs else { return }
        watchedPairs = pairs
        watcher = FolderWatcher(paths: pairs.flatMap { [$0.local, $0.onedrive] }) { [weak self] paths in
            Task { @MainActor in self?.foldersChanged(paths) }
        }
    }

    private static func isInside(_ path: String, _ root: String) -> Bool {
        let folder = root.hasSuffix("/") ? root : root + "/"
        return path == root || path.hasPrefix(folder)
    }

    private func foldersChanged(_ paths: [String]) {
        // Ignore the echo of SyncBridge's own copies.
        guard !isSyncing, Date().timeIntervalSince(lastSyncEnd) > 10 else { return }
        for pair in watchedPairs where paths.contains(where: { Self.isInside($0, pair.local) || Self.isInside($0, pair.onedrive) }) {
            changedPairs.insert(pair.name)
        }
        guard !changedPairs.isEmpty else { return }
        let delay = UserDefaults.standard.object(forKey: Self.realtimeDelayKey) as? Int ?? 30
        realtimeTimer?.invalidate()
        realtimeTimer = Timer.scheduledTimer(withTimeInterval: TimeInterval(max(delay, 5)), repeats: false) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                let pairs = Array(self.changedPairs)
                self.changedPairs.removeAll()
                await self.syncNow(pairs: pairs)
            }
        }
    }

    func loadConflicts() async {
        do {
            conflicts = try await engine.decode([Conflict].self, ["--list-conflicts"])
            let open = Set(conflicts.map(\.id))
            savedSuggestions = savedSuggestions.filter { open.contains($0.key) }
            refreshSuggestions()
        } catch {
            message = error.localizedDescription
        }
    }

    func loadConflictTexts(_ conflict: Conflict) async throws -> ConflictTexts {
        // Long enough for whoever reads it; each prompt shortens to its own model's size.
        let limit = String(max(AIAssistant.decisionMaxChars, AIAssistant.provider.maxCharsPerVersion))
        return try await engine.decode(
            ConflictTexts.self, ["--conflict-texts", conflict.pair, conflict.file, "--max-chars", limit]
        )
    }

    // MARK: AI suggestions

    /// Conflicts the AI may decide: both versions exist and the names match.
    /// Deletions, name conflicts and merges always wait for the user.
    static func aiCanDecide(_ conflict: Conflict) -> Bool {
        conflict.local != nil && conflict.onedrive != nil && !conflict.isNameConflict
    }

    /// Shows the saved answers that still fit: same file versions, same instructions.
    func refreshSuggestions() {
        aiSuggestions = Dictionary(uniqueKeysWithValues: conflicts.compactMap { conflict in
            savedSuggestions[conflict.id].flatMap { $0.fits(conflict) ? (conflict.id, $0.decision) : nil }
        })
    }

    @discardableResult
    func suggest(_ conflict: Conflict) async throws -> AIDecision {
        aiAsking.insert(conflict.id)
        defer { aiAsking.remove(conflict.id) }
        let texts = try await loadConflictTexts(conflict)
        let decision = try await AIAssistant().decide(texts, local: conflict.local, onedrive: conflict.onedrive)
        aiSuggestions[conflict.id] = decision
        savedSuggestions[conflict.id] = SavedSuggestion(
            decision: decision, local: conflict.local, onedrive: conflict.onedrive,
            instructions: AITrust.currentFingerprint
        )
        AITrust.save(savedSuggestions, to: AITrust.supportFile("ai-suggestions.json"))
        return decision
    }

    /// The AI provider's own view of a conflict, with reasons, e.g. to explain what Jev decided.
    /// Not saved and not counted: Jev's suggestion stays the one that is measured.
    func explain(_ conflict: Conflict) async throws -> AIDecision {
        let texts = try await loadConflictTexts(conflict)
        return try await AIAssistant().decide(texts, local: conflict.local, onedrive: conflict.onedrive, useJev: false)
    }

    /// Asks the AI about every open conflict it may decide, a few at a time, skipping those it
    /// already answered. Returns an error text if one failed; then no further files are started.
    /// notify: a notification says when a long run is done.
    func suggestAll(limit: Int = 30, notify: Bool = true) async -> String? {
        let queue = conflicts.filter(Self.aiCanDecide).filter { aiSuggestions[$0.id] == nil }.prefix(limit)
        guard !queue.isEmpty else { return nil }
        let started = Date()
        aiStopRequested = false
        aiProgress = (0, queue.count)
        defer { aiProgress = nil }
        aiQueue = Array(queue)
        aiFailure = nil
        let workers = (0..<AIAssistant.decisionParallelRequests).map { _ in Task { await aiWorker() } }
        for worker in workers { await worker.value }
        let failure = aiFailure
        if notify, let failure, Date().timeIntervalSince(started) > 20 {
            post(EngineNotification(title: "AI help stopped", message: failure, open: "conflicts"))
        } else if notify, Date().timeIntervalSince(started) > 20 {
            let unsure = queue.filter { aiSuggestions[$0.id]?.keep == "ask" }.count
            post(EngineNotification(
                title: "AI suggestions ready",
                message: "The AI answered \(aiProgress?.done ?? queue.count) conflict(s)" + (unsure > 0 ? "; \(unsure) it left for you." : ".")
                    + " Check them in the Conflicts window.",
                open: "conflicts"
            ))
        }
        return failure
    }

    /// Takes the next waiting conflict until none are left, the user stops or one fails.
    private func aiWorker() async {
        while !aiStopRequested, aiFailure == nil, !aiQueue.isEmpty {
            let conflict = aiQueue.removeFirst()
            do { try await suggest(conflict) } catch { aiFailure = aiFailure ?? error.localizedDescription }
            if let progress = aiProgress { aiProgress = (progress.done + 1, progress.total) }
        }
    }

    /// Stops "Ask AI for All" after the answers it is waiting for.
    func stopAI() { aiStopRequested = true }

    /// After a sync, with automatic decisions on: the AI resolves the conflicts it is sure about.
    private func decideAutomatically() async {
        let trust = AITrust.shared
        guard trust.autoDecide, trust.isEligible, AIAssistant.canDecide, !conflicts.isEmpty else { return }
        _ = await suggestAll(notify: false)
        let batch = conflicts.filter(Self.aiCanDecide).compactMap { conflict -> ConflictDecision? in
            guard let keep = aiSuggestions[conflict.id]?.keep, keep != "ask" else { return nil }
            return ConflictDecision(pair: conflict.pair, file: conflict.file, keep: keep)
        }
        guard !batch.isEmpty else { return }
        let text = await resolve(batch)
        post(EngineNotification(
            title: "AI resolved \(batch.count) conflict(s)",
            message: text + " " + batch.prefix(3).map { ($0.file as NSString).lastPathComponent }.joined(separator: ", "),
            open: "conflicts"
        ))
    }

    func loadDeletions() async {
        do {
            deletionGroups = try await engine.decode([DeletionGroup].self, ["--list-deletions"])
        } catch {
            message = error.localizedDescription
        }
    }

    /// Applies conflict decisions in one batch; returns a short result text.
    func resolve(_ decisions: [ConflictDecision]) async -> String {
        isWorking = true
        defer { isWorking = false }
        do {
            let result = try await engine.runWithFile("--resolve-batch", payload: decisions)
            await loadConflicts()
            await refreshStatus()
            return summary(result, verb: "Resolved")
        } catch {
            return error.localizedDescription
        }
    }

    func applyDeletions(_ items: [DeletionItem], restore: Bool) async -> String {
        isWorking = true
        defer { isWorking = false }
        do {
            let result = try await engine.runWithFile(
                restore ? "--restore-deletions" : "--apply-deletions",
                payload: items,
                extra: ["--confirmed"]
            )
            await loadDeletions()
            await refreshStatus()
            return summary(result, verb: restore ? "Restored" : "Deleted")
        } catch {
            return error.localizedDescription
        }
    }

    private func summary(_ result: BatchResult, verb: String) -> String {
        if let error = result.error { return error }
        var text = "\(verb) \(result.done?.count ?? 0) file(s)."
        if let failed = result.failed, !failed.isEmpty {
            text += "\n\(failed.count) could not be processed:\n"
            text += failed.prefix(5).map(\.text).joined(separator: "\n")
        }
        if let activated = result.activated, !activated.isEmpty {
            text += "\nNow excluded: " + activated.joined(separator: ", ")
        }
        return text
    }

    // MARK: Helpers

    func open(_ path: String?) {
        guard let path else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    /// Set by the menu bar icon, which is always on screen, so windows can be
    /// opened from outside SwiftUI views (e.g. a notification click).
    var windowOpener: ((String) -> Void)?

    /// Shows an app window ("conflicts", "deletions", "preview", "settings") or the last summary.
    func show(_ target: String) {
        if target == "ai" {
            settingsTab = "ai"
            show("settings")
            return
        }
        if target == "summary" {
            open(status?.summaryFile)
            return
        }
        windowOpener?(target)
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// Shows notifications even while a SyncBridge window is in front, and opens
/// the matching window when one is clicked.
final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    var onOpen: ((String) -> Void)?

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let target = response.notification.request.content.userInfo["open"] as? String ?? "summary"
        DispatchQueue.main.async { self.onOpen?(target) }
        completionHandler()
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}
