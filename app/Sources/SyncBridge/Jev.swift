import Foundation

/// Jev, TypeSafe's decision model, reached through OpenRouter's Decisions API with the
/// user's OpenRouter key. It answers one typed question (which version to keep) with a
/// probability per answer, in well under a second. It writes no text; its reasons are its
/// answers to fixed side questions (see reasonQuestions), and it cannot merge.
enum Jev {
    static let url = URL(string: "https://openrouter.ai/api/alpha/decisions")!
    /// Always the newest Jev release.
    static let latestModel = "~typesafe/jev-latest"
    /// Below this probability the answer becomes "ask": the user decides.
    static let minimumProbability = 0.8
    static let enabledKey = "aiJevDecides"
    static let modelKey = AIAssistant.modelKeyPrefix + "jev"
    static let serviceName = "OpenRouter and TypeSafe"
    static let maxCharsPerVersion = 60_000

    /// Jev decides conflicts; the AI provider still writes merges, explanations and instructions.
    static var isOn: Bool { UserDefaults.standard.bool(forKey: enabledKey) }

    static var model: String {
        let saved = UserDefaults.standard.string(forKey: modelKey) ?? ""
        return saved.isEmpty ? latestModel : saved
    }

    /// Jev runs on the OpenRouter key, shared with the OpenRouter provider.
    static var key: String? { KeychainStore.key(for: .openrouter) }

    static let options: [String: String] = [
        "local": "Keep the laptop version. The OneDrive version has nothing important that the laptop version lacks.",
        "onedrive": "Keep the OneDrive version. The laptop version has nothing important that the OneDrive version lacks.",
        "ask": "Both versions have their own important changes, or it is unclear which one is right. The user must decide.",
    ]

    static func question(instructions: String) -> [String: Any] {
        var text = """
        The same file was changed on the user's laptop and in OneDrive. Which version should the \
        user keep? Never lose content: if each version has something the other lacks, answer ask.
        """
        if !instructions.isEmpty { text += "\nThe user's instructions, which take priority:\n\(instructions)" }
        return ["type": "choice", "instructions": text, "criteria": options]
    }

    // MARK: Reasons

    /// The side questions. Jev answers them in the same request as the decision, each with a
    /// probability; the clear answers become the reasons the user reads.
    private static let sideScale = [
        "The laptop version, clearly", "The laptop version, slightly", "Neither: they are about equal",
        "The OneDrive version, slightly", "The OneDrive version, clearly",
    ]
    static let reasonQuestions: [String: [String: Any]] = [
        "covers_local": ["type": "noul", "instructions": "Does the laptop version contain all content of substance that the OneDrive version has?"],
        "covers_onedrive": ["type": "noul", "instructions": "Does the OneDrive version contain all content of substance that the laptop version has?"],
        "fuller": [
            "type": "score", "criteria": sideScale,
            "instructions": "Which version is more comprehensive: has more content, sections, detail?",
        ],
        "quality": [
            "type": "score", "criteria": sideScale,
            "instructions": "Which version is better finished: fewer placeholders, typos, broken sentences, leftover drafts, errors?",
        ],
        "cosmetic": ["type": "noul", "instructions": "Do the two versions differ only in formatting, spacing, order, dates or metadata, and not in substance?"],
        "both_unique": ["type": "noul", "instructions": "Does each version have important content that the other one lacks?"],
        "broken": [
            "type": "choice",
            "instructions": "Does one version look truncated, half deleted, corrupted or emptied by mistake?",
            "criteria": [
                "none": "Neither version looks damaged.",
                "local": "The laptop version looks damaged or incomplete.",
                "onedrive": "The OneDrive version looks damaged or incomplete.",
            ],
        ],
        "later": [
            "type": "choice",
            "instructions": "Is one version a later revision that builds on the other?",
            "criteria": [
                "local": "The laptop version continues from the OneDrive version.",
                "onedrive": "The OneDrive version continues from the laptop version.",
                "parallel": "They were edited separately from a common older version.",
            ],
        ],
    ]

    /// One clear answer to a side question, and the version it speaks for (nil: neither).
    struct Finding {
        var text: String
        var favours: String?
        /// True when it says both versions matter, which speaks against keeping only one.
        var needsBoth = false
    }

    private static let sides = ["local": "Laptop", "onedrive": "OneDrive"]
    private static func other(_ side: String) -> String { side == "local" ? "onedrive" : "local" }
    private static func percent(_ probability: Double) -> Int { Int((probability * 100).rounded()) }

    /// What Jev is clear about, from the answers to the side questions. Unclear answers,
    /// refusals and missing answers are left out.
    static func findings(in answers: [String: Any]) -> [Finding] {
        func answer(_ key: String, _ type: String) -> [String: Any]? {
            let answer = answers[key] as? [String: Any]
            return answer?["type"] as? String == type ? answer : nil
        }
        func yes(_ key: String) -> Double { answer(key, "noul")?["noul"] as? Double ?? 0 }
        /// The chosen option, if Jev is at least 80% sure of it.
        func choice(_ key: String) -> (option: String, probability: Double)? {
            guard let answer = answer(key, "choice"), let option = answer["choice"] as? String,
                  let probability = (answer["probabilities"] as? [String: Any])?[option] as? Double,
                  probability >= minimumProbability else { return nil }
            return (option, probability)
        }
        /// Which side a five-step scale leans to and how far (0 to 2), from `least` on.
        func lean(_ key: String, least: Double) -> (side: String, clearly: Bool)? {
            guard let score = answer(key, "score")?["score"] as? Double, abs(score - 2) >= least else { return nil }
            return (score < 2 ? "local" : "onedrive", abs(score - 2) >= 1.5)
        }

        var found: [Finding] = []
        let covers = ["local": yes("covers_local"), "onedrive": yes("covers_onedrive")]
        let bothUnique = yes("both_unique") >= 0.5
        if yes("cosmetic") >= 0.7 {
            found.append(Finding(text: "The versions differ only in formatting or details (\(percent(yes("cosmetic")))%)."))
        } else if !bothUnique {
            // Left out when Jev also says each side has its own content: the cautious answer wins.
            for (side, name) in sides.sorted(by: { $0.key < $1.key })
            where covers[side]! >= minimumProbability && covers[other(side)]! < minimumProbability {
                found.append(Finding(
                    text: "\(name) has everything the other version has (\(percent(covers[side]!))%).", favours: side
                ))
            }
        }
        if let fuller = lean("fuller", least: 0.75) {
            found.append(Finding(
                text: "\(sides[fuller.side]!) is \(fuller.clearly ? "clearly" : "somewhat") more comprehensive.",
                favours: fuller.side
            ))
        }
        // Only a clear lead counts: a slight one mostly repeats "more comprehensive".
        if let quality = lean("quality", least: 1.5) {
            found.append(Finding(text: "\(sides[quality.side]!) is better finished.", favours: quality.side))
        }
        if let broken = choice("broken"), let name = sides[broken.option] {
            found.append(Finding(
                text: "\(name) looks damaged or incomplete (\(percent(broken.probability))%).",
                favours: other(broken.option)
            ))
        }
        if let later = choice("later") {
            if let name = sides[later.option] {
                found.append(Finding(
                    text: "\(name) builds on the other version (\(percent(later.probability))%).", favours: later.option
                ))
            } else {
                found.append(Finding(text: "Both were edited separately (\(percent(later.probability))%).", needsBoth: true))
            }
        }
        if bothUnique {
            found.append(Finding(
                text: "Each version has content the other lacks (\(percent(yes("both_unique")))%).", needsBoth: true
            ))
        }
        return found
    }

    /// The findings as lines under a decision. For a kept version, what speaks against it says "But".
    static func reasonLines(for keep: String, in answers: [String: Any]) -> String {
        let found = findings(in: answers)
        guard !found.isEmpty else { return keep == "ask" ? "" : "\nJev found no clear reason." }
        return found.map { finding in
            let against = keep != "ask" && (finding.needsBoth || (finding.favours ?? keep) != keep)
            return "\n• " + (against ? "But: " : "") + finding.text
        }.joined()
    }

    static func decide(state: String, instructions: String) async throws -> AIDecision {
        guard let key else { throw AIError.notConfigured("Add your OpenRouter API key for Jev in Settings › AI.") }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 60
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var questions = reasonQuestions
        questions["keep"] = question(instructions: instructions)
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": model, "state": state, "questions": questions,
        ] as [String: Any])
        let json = try await APICall.send(request, provider: .openrouter, feature: "Jev")
        guard let answers = json?["answers"] as? [String: Any], answers["keep"] is [String: Any] else {
            throw AIError.failed("""
                Jev answered in an unexpected format, so its answer can't be used. OpenRouter's Decisions API \
                may have changed; try again later or turn Jev off in Settings › AI.
                """)
        }
        return parse(answers)
    }

    /// Turns Jev's answers into a decision with reasons; unsure answers and refusals become "ask".
    static func parse(_ answers: [String: Any]) -> AIDecision {
        guard let answer = answers["keep"] as? [String: Any], answer["type"] as? String == "choice",
              let choice = answer["choice"] as? String, options[choice] != nil else {
            return AIDecision(keep: "ask", reason: "Jev gave no answer for this file.")
        }
        let probability = (answer["probabilities"] as? [String: Any])?[choice] as? Double ?? 0
        let title = AIDecision(keep: choice, reason: "").title
        if choice == "ask" {
            return AIDecision(
                keep: "ask",
                reason: "Jev thinks you should decide (\(percent(probability))% sure)." + reasonLines(for: "ask", in: answers)
            )
        }
        if probability < minimumProbability {
            return AIDecision(
                keep: "ask",
                reason: "Jev leans to \(title) but is only \(percent(probability))% sure, so you decide."
                    + reasonLines(for: "ask", in: answers)
            )
        }
        return AIDecision(
            keep: choice, reason: "Jev: \(title), \(percent(probability))% sure." + reasonLines(for: choice, in: answers)
        )
    }
}
