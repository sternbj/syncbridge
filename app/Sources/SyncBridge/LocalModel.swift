import Foundation

/// A model running on this Mac or the local network (Ollama, LM Studio, llama.cpp …),
/// reached through the OpenAI-style Chat Completions API, as in Folio.
///
/// Plain http is accepted only for this Mac and the local network; anything else
/// needs https, so file text never travels unencrypted over the internet.
enum LocalModel {
    static let addressKey = "aiLocalAddress"
    static let defaultAddress = "http://localhost:11434/v1"

    static let servers: [(name: String, address: String)] = [
        ("Ollama", "http://localhost:11434/v1"),
        ("LM Studio", "http://localhost:1234/v1"),
        ("llama.cpp", "http://localhost:8080/v1"),
    ]

    static var address: String {
        let saved = UserDefaults.standard.string(forKey: addressKey) ?? ""
        return saved.isEmpty ? defaultAddress : saved
    }

    static var baseURL: URL? { validURL(address) }

    /// "localhost:11434"
    static var serverName: String {
        guard let url = baseURL, let host = url.host else { return "your local model server" }
        return url.port.map { "\(host):\($0)" } ?? host
    }

    /// The address as a URL, or nil when it is malformed or uses http for a public host.
    static func validURL(_ text: String) -> URL? {
        let trimmed = text.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: trimmed), let scheme = url.scheme, let host = url.host,
              url.user == nil, url.password == nil, url.query == nil else { return nil }
        if scheme == "https" { return url }
        return scheme == "http" && isLocal(host) ? url : nil
    }

    /// This Mac, a .local name or a private network address.
    static func isLocal(_ host: String) -> Bool {
        let host = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if host == "localhost" || host.hasSuffix(".local") || host == "::1" { return true }
        if host.hasPrefix("fe80:") || host.hasPrefix("fc") || host.hasPrefix("fd") { return host.contains(":") }
        let parts = host.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4 else { return false }
        switch (parts[0], parts[1]) {
        case (127, _), (10, _), (192, 168), (169, 254): return true
        case (172, 16...31): return true
        default: return false
        }
    }

    /// The server's models (GET /models). Some servers have no list; then a typed name still works.
    static func models(at url: URL, key: String?) async throws -> [String] {
        var request = URLRequest(url: url.appendingPathComponent("models"))
        request.timeoutInterval = 10
        if let key, !key.isEmpty { request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw AIError.failed("Couldn't reach \(url.host ?? "the server"). Check that the server is running and the address is right.")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 404 || status == 405 { return [] }
        guard status == 200 else { throw AIError.failed("The server answered with error \(status).") }
        let list = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["data"] as? [[String: Any]] ?? []
        return list.compactMap { $0["id"] as? String }.sorted()
    }
}
