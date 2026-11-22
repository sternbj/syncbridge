import Foundation

/// Calls to the API-key services (Anthropic, OpenAI, OpenRouter, a local server) and plain-language
/// explanations when they fail: what happened, the likely cause and what to do next.
enum APICall {
    /// Sends the request and returns the JSON body of a successful answer; otherwise throws an
    /// explained error. `feature` names what was used when it isn't the provider itself (e.g. "Jev").
    static func send(_ request: URLRequest, provider: AIProvider, feature: String? = nil) async throws -> [String: Any]? {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw AIError.failed(explain(networkError: error, provider: provider))
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard status == 200 else {
            var keyWorks: Bool?
            if status == 401 || status == 403, provider == .openrouter, feature != nil,
               let key = request.value(forHTTPHeaderField: "Authorization")?.replacingOccurrences(of: "Bearer ", with: "") {
                keyWorks = await openRouterKeyWorks(key)
            }
            throw AIError.failed(explain(status: status, data: data, provider: provider, feature: feature,
                                         model: model(of: request), keyWorks: keyWorks))
        }
        return json
    }

    // MARK: Explanations

    static func explain(status: Int, data: Data, provider: AIProvider, feature: String? = nil,
                        model: String? = nil, keyWorks: Bool? = nil) -> String {
        let name = provider.name
        let what = feature ?? name
        let keys = provider.keyURL.isEmpty ? "" : " (\(display(provider.keyURL)))"
        let modelName = model.map { "“\($0)”" } ?? "the model"
        var text: String
        switch status {
        case 400:
            text = "\(name) rejected the request (error 400). Usually the model name \(modelName) is misspelled "
                + "or the model can't handle this request. Check the model name in Settings › AI."
        case 401 where keyWorks == true, 403 where keyWorks == true:
            text = "Your \(name) key is valid, but \(what) refused it (error \(status)). Your account may not have "
                + "access to \(what) yet, or the key has a restriction. Check your account at \(display(provider.keyURL))."
        case 401:
            text = "\(name) didn't accept the API key (error 401). The key is wrong, incomplete, deleted or disabled."
            if provider == .openrouter {
                text += " Management (provisioning) keys can't run models either."
            }
            text += " Create a new key\(keys), paste it here and click Save Key, then test again."
        case 402:
            text = "Your \(name) balance is used up (error 402). Add credits at \(display(provider.billingURL)), then test again."
        case 403:
            text = "\(name) refused the request (error 403). The key works but isn't allowed to do this: "
                + "it may have hit its spending limit, or your account has no access to \(modelName)."
            if !provider.keyURL.isEmpty { text += " Check the key's limits at \(display(provider.keyURL))." }
        case 404:
            text = "\(name) doesn't know \(modelName) (error 404). Check the spelling of the model name in Settings › AI."
        case 408, 504:
            text = "\(name) took too long to answer (error \(status)). Try again in a moment."
        case 413:
            text = "The file is too large for \(name) (error 413). Try a model with a bigger context."
        case 429:
            text = "Too many requests or quota used up at \(name) (error 429). Wait a minute and try again; "
                + "if it keeps happening, check your plan and billing at \(display(provider.billingURL))."
        case 500..<600:
            text = "\(name) has a problem on its side (error \(status)). This isn't your key; try again later."
        default:
            text = "\(name) answered with error \(status)."
        }
        if let detail = serviceMessage(data) { text += "\n\(name) said: \(detail)" }
        return text
    }

    static func explain(networkError error: Error, provider: AIProvider) -> String {
        let code = (error as? URLError)?.code
        switch code {
        case .notConnectedToInternet, .networkConnectionLost:
            return "No internet connection. Connect to the internet and try again."
        case .timedOut:
            return "\(provider.name) didn't answer in time. Try again in a moment."
        case .cannotFindHost, .cannotConnectToHost, .dnsLookupFailed:
            return provider == .local
                ? "Couldn't reach your local model server. Check that it is running and the address is right."
                : "Couldn't reach \(provider.name). Check your internet connection, VPN or firewall."
        default:
            return "Couldn't reach \(provider.name): \(error.localizedDescription)"
        }
    }

    /// Catches keys that can't work before they are saved: a key for another service, spaces, cut off.
    static func keyProblem(_ key: String, for provider: AIProvider) -> String? {
        let key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        // Local servers accept any key, often a short placeholder.
        guard !key.isEmpty, provider != .local else { return nil }
        if key.contains(where: \.isWhitespace) {
            return "The key contains spaces or line breaks, so it was probably copied with extra text. Copy only the key."
        }
        let owner: AIProvider? = key.hasPrefix("sk-ant-") ? .anthropic : key.hasPrefix("sk-or-") ? .openrouter
            : key.hasPrefix("sk-") ? .openai : nil
        if let owner, owner != provider {
            return "This looks like an \(owner.name) key, not an \(provider.name) key. "
                + "Get an \(provider.name) key at \(display(provider.keyURL))."
        }
        if key.count < 20 { return "The key is too short; it was probably copied incompletely." }
        return nil
    }

    // MARK: Helpers

    /// The service's own error text, if any.
    private static func serviceMessage(_ data: Data) -> String? {
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let error = json?["error"]
        let message = (error as? [String: Any])?["message"] as? String ?? error as? String ?? json?["message"] as? String
            ?? String(data: data, encoding: .utf8).map { String($0.prefix(300)) }
        let trimmed = message?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty || trimmed.hasPrefix("<") ? nil : trimmed
    }

    private static func model(of request: URLRequest) -> String? {
        guard let body = request.httpBody else { return nil }
        return ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["model"] as? String
    }

    private static func display(_ url: String) -> String {
        url.replacingOccurrences(of: "https://", with: "")
    }

    /// Whether OpenRouter accepts the key at all, to tell a bad key from a refused feature.
    private static func openRouterKeyWorks(_ key: String) async -> Bool? {
        var request = URLRequest(url: URL(string: "https://openrouter.ai/api/v1/key")!)
        request.timeoutInterval = 10
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        guard let (_, response) = try? await URLSession.shared.data(for: request),
              let status = (response as? HTTPURLResponse)?.statusCode else { return nil }
        return status == 200 ? true : status == 401 ? false : nil
    }
}

extension KeychainStore {
    /// Saves the key unless it can't work; returns why it wasn't saved.
    static func setCheckedKey(_ key: String, for provider: AIProvider) -> String? {
        if let problem = APICall.keyProblem(key, for: provider) { return "Key not saved. \(problem)" }
        setKey(key, for: provider)
        return nil
    }
}

extension AIProvider {
    var billingURL: String {
        switch self {
        case .anthropic: return "https://console.anthropic.com/settings/billing"
        case .openai: return "https://platform.openai.com/settings/organization/billing"
        case .openrouter: return "https://openrouter.ai/settings/credits"
        default: return keyURL
        }
    }
}
