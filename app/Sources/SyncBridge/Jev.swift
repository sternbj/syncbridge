import Foundation

/// Jev, TypeSafe's decision model, reached through OpenRouter's Decisions API with the
/// user's OpenRouter key. It answers one typed question (which version to keep) with a
/// probability per answer, in well under a second, but writes no text: no reasons, no merges.
enum Jev {
    static let url = URL(string: "https://openrouter.ai/api/alpha/decisions")!
    /// Always the newest Jev release.
    static let latestModel = "~typesafe/jev-latest"
    /// Below this probability the answer becomes "ask": the user decides.
    static let minimumProbability = 0.8

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

    static func decide(state: String, instructions: String, model: String, key: String) async throws -> AIDecision {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 60
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": model, "state": state, "questions": ["keep": question(instructions: instructions)],
        ] as [String: Any])
        let (data, response) = try await URLSession.shared.data(for: request)
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            let detail = (json?["error"] as? [String: Any])?["message"] as? String
                ?? String(data: data, encoding: .utf8)?.prefix(300).description ?? ""
            throw AIError.failed("Jev returned an error: \(detail)")
        }
        return parse((json?["answers"] as? [String: Any])?["keep"] as? [String: Any])
    }

    /// Turns Jev's answer into a decision; unsure answers and refusals become "ask".
    static func parse(_ answer: [String: Any]?) -> AIDecision {
        guard let answer, answer["type"] as? String == "choice", let choice = answer["choice"] as? String,
              options[choice] != nil else {
            return AIDecision(keep: "ask", reason: "Jev gave no answer for this file.")
        }
        let probability = (answer["probabilities"] as? [String: Any])?[choice] as? Double ?? 0
        let percent = Int((probability * 100).rounded())
        let title = AIDecision(keep: choice, reason: "").title
        if choice == "ask" {
            return AIDecision(keep: "ask", reason: "Jev thinks you should decide (\(percent)% sure).")
        }
        if probability < minimumProbability {
            return AIDecision(keep: "ask", reason: "Jev leans to \(title) but is only \(percent)% sure, so you decide.")
        }
        return AIDecision(keep: choice, reason: "Jev: \(title), \(percent)% sure. Jev gives no reasons.")
    }
}
