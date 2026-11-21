import AppKit
import CryptoKit
import Foundation
import Network
import Security

// Use an existing ChatGPT or Claude plan instead of an API key, the way the
// open-source PDF reader Folio does it:
// - ChatGPT: OpenAI's "Sign in with ChatGPT" for open-source apps (OAuth 2.0 + PKCE with a
//   loopback redirect). Requests count toward the person's ChatGPT plan.
//   https://developers.openai.com/siwc/token-sharing-open-source/sign-in
// - Claude: the person's own Claude Code, signed in with their Claude plan through Anthropic's
//   own flow. SyncBridge never sees Claude credentials; it runs `claude -p` with every tool off.

// MARK: - ChatGPT

struct ChatGPTSession: Codable {
    var accessToken: String
    var refreshToken: String
    var idToken: String
    var expiresAt: Date
    var email: String
}

struct ChatGPTModel: Identifiable, Hashable {
    let id: String
    let name: String
}

actor ChatGPTAccount {
    static let shared = ChatGPTAccount()

    private static let issuer = "https://auth.openai.com"
    private static let authorizeURL = "https://auth.openai.com/api/accounts/authorize"
    private static let tokenURL = "https://auth.openai.com/api/accounts/oauth/token"
    private static let api = "https://api.openai.com/v1"
    private static let scopes = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    static let usageURL = URL(string: "https://chatgpt.com/settings/usage")!
    private static let sessionAccount = "chatgpt-session"
    private static let hostIdKey = "chatgptHostId"
    private static let clientIdKey = "chatgptClientId"
    private static let refreshFailures: Set<String> = [
        "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
        "refresh_token_invalidated", "refresh_token_reused",
    ]

    private var refreshTask: Task<String, Error>?
    private var signInTask: Task<ChatGPTSession, Error>?
    private var signInServer: LoopbackServer?
    /// The sign-in page of the running sign-in, to open again or copy into another browser.
    private(set) var signInPage: URL?

    static var session: ChatGPTSession? {
        KeychainStore.read(sessionAccount).flatMap { try? JSONDecoder().decode(ChatGPTSession.self, from: $0) }
    }

    private static func save(_ session: ChatGPTSession?) {
        KeychainStore.write(session.flatMap { try? JSONEncoder().encode($0) }, account: sessionAccount)
    }

    /// Identifies this Mac's SyncBridge to OpenAI; the client ID is issued on the first sign-in.
    private static var hostId: String {
        if let id = UserDefaults.standard.string(forKey: hostIdKey) { return id }
        let id = "urn:uuid:\(UUID().uuidString.lowercased())"
        UserDefaults.standard.set(id, forKey: hostIdKey)
        return id
    }

    private static var clientId: String? {
        get { UserDefaults.standard.string(forKey: clientIdKey) }
        set { UserDefaults.standard.set(newValue, forKey: clientIdKey) }
    }

    static func signedOut(_ text: String = "Continue with ChatGPT in Settings › AI to use your plan.") -> AIError {
        .notConfigured(text)
    }

    // MARK: Sign in and out

    /// Opens the browser for "Continue with ChatGPT" and waits for the answer.
    func signIn() async throws -> ChatGPTSession {
        if let signInTask { return try await signInTask.value }
        let task = Task { try await self.runSignIn() }
        signInTask = task
        defer { signInTask = nil }
        return try await task.value
    }

    /// Stops a sign-in that waits for the browser, e.g. after the wrong browser profile opened.
    func cancelSignIn() {
        signInServer?.stop()
    }

    private func runSignIn() async throws -> ChatGPTSession {
        let verifier = Self.randomToken(32)
        let challenge = Self.base64URL(Data(SHA256.hash(data: Data(verifier.utf8))))
        let state = Self.randomToken(24)
        let nonce = Self.randomToken(24)

        // Port 1455 is the documented default; any free loopback port also works.
        let server = try await LoopbackServer.start(preferredPort: 1455, expectedState: state)
        signInServer = server
        defer {
            server.stop()
            signInServer = nil
            signInPage = nil
        }
        let redirectURI = "http://127.0.0.1:\(server.port)/auth/callback"
        var items = [
            "response_type": "code",
            "client_id": Self.clientId ?? "dynamic_agent_client",
            "redirect_uri": redirectURI,
            "scope": Self.scopes,
            "resource": Self.api,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "ext_agent_host_id": Self.hostId,
        ]
        if Self.clientId == nil { items["agent_name_hint"] = "SyncBridge" }
        var components = URLComponents(string: Self.authorizeURL)!
        components.queryItems = items.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        let page = components.url!
        signInPage = page
        await MainActor.run { _ = NSWorkspace.shared.open(page) }

        let query = try await server.waitForCallback(timeout: 300)
        if let error = query["error"] {
            throw AIError.failed(error == "access_denied" ? "Sign-in was cancelled." : "ChatGPT sign-in failed.")
        }
        guard let clientId = Self.clientId ?? query["client_id"], let code = query["code"] else {
            throw AIError.failed("ChatGPT didn't finish registering SyncBridge. Please try again.")
        }
        // Keep the issued client ID for every later sign-in on this Mac, even if the rest fails.
        Self.clientId = clientId
        let (ok, tokens) = try await Self.postForm(Self.tokenURL, [
            "grant_type": "authorization_code", "client_id": clientId, "code": code,
            "code_verifier": verifier, "redirect_uri": redirectURI, "resource": Self.api,
        ])
        guard ok, let access = tokens["access_token"] as? String, let idToken = tokens["id_token"] as? String else {
            throw AIError.failed("ChatGPT sign-in failed. Please try again.")
        }
        let claims = try await Self.verifyIdToken(idToken, clientId: clientId, nonce: nonce)
        let granted = ((tokens["scope"] as? String) ?? query["scope"] ?? "")
            .split(whereSeparator: { $0 == " " || $0 == "+" }).map(String.init)
        guard granted.contains("chatgpt.tokens.use.direct") else {
            throw AIError.failed("ChatGPT plan usage wasn't granted. Sign in again and allow SyncBridge to use your plan.")
        }
        let session = ChatGPTSession(
            accessToken: access,
            refreshToken: tokens["refresh_token"] as? String ?? "",
            idToken: idToken,
            expiresAt: Date().addingTimeInterval(tokens["expires_in"] as? Double ?? 3600),
            email: claims["email"] as? String ?? ""
        )
        Self.save(session)
        return session
    }

    func signOut() async {
        _ = try? await refreshTask?.value
        let current = Self.session
        Self.save(nil)
        guard let refresh = current?.refreshToken, !refresh.isEmpty,
              let config = try? await Self.openIdConfig(),
              let revoke = config["revocation_endpoint"] as? String else { return }
        // The tokens are already gone locally; access can also be removed in ChatGPT's settings.
        _ = try? await Self.postForm(revoke, [
            "token": refresh, "token_type_hint": "refresh_token", "client_id": Self.clientId ?? "",
        ])
    }

    // MARK: Tokens

    private func accessToken(force: Bool = false) async throws -> String {
        guard let current = Self.session else { throw Self.signedOut() }
        if !force, current.expiresAt.timeIntervalSinceNow > 60 { return current.accessToken }
        // Refresh tokens rotate: never run two refreshes at once.
        if let refreshTask { return try await refreshTask.value }
        let task = Task { try await Self.refresh(current) }
        refreshTask = task
        defer { refreshTask = nil }
        return try await task.value
    }

    private static func refresh(_ current: ChatGPTSession) async throws -> String {
        let (ok, json) = try await postForm(tokenURL, [
            "grant_type": "refresh_token", "client_id": clientId ?? "",
            "refresh_token": current.refreshToken, "resource": api,
        ])
        guard ok, let access = json["access_token"] as? String else {
            if let code = json["error"] as? String, refreshFailures.contains(code) {
                save(nil)
                throw signedOut("Your ChatGPT sign-in has ended. Continue with ChatGPT in Settings › AI to reconnect.")
            }
            throw AIError.failed("Couldn't refresh your ChatGPT sign-in. Check your connection and try again.")
        }
        var next = current
        next.accessToken = access
        next.refreshToken = json["refresh_token"] as? String ?? current.refreshToken
        next.idToken = json["id_token"] as? String ?? current.idToken
        next.expiresAt = Date().addingTimeInterval(json["expires_in"] as? Double ?? 3600)
        save(next)
        return access
    }

    // MARK: Requests

    func models() async throws -> [ChatGPTModel] {
        var request = URLRequest(url: URL(string: "\(Self.api)/models")!)
        request.setValue("Bearer \(try await accessToken())", forHTTPHeaderField: "Authorization")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw AIError.failed("Couldn't load ChatGPT models.")
        }
        let list = (json["models"] as? [[String: Any]]) ?? (json["data"] as? [[String: Any]]) ?? []
        return list.compactMap { model in
            if let visibility = model["visibility"] as? String, visibility != "list" { return nil }
            guard let id = (model["slug"] ?? model["id"]) as? String else { return nil }
            return ChatGPTModel(id: id, name: model["display_name"] as? String ?? id)
        }
    }

    /// quick: little reasoning, for short yes/no style answers such as conflict decisions.
    func complete(system: String, prompt: String, model: String, quick: Bool = false) async throws -> String {
        var chosen = model
        if chosen.isEmpty {
            guard let first = try await models().first else { throw AIError.failed("No ChatGPT model is available.") }
            chosen = first.id
        }
        var fields: [String: Any] = [
            "model": chosen, "instructions": system, "store": false, "stream": true,
            "input": [["role": "user", "content": prompt]],
        ]
        if quick { fields["reasoning"] = ["effort": "low"] }
        var body = try JSONSerialization.data(withJSONObject: fields)
        func send(_ token: String) async throws -> (URLSession.AsyncBytes, HTTPURLResponse) {
            var request = URLRequest(url: URL(string: "\(Self.api)/responses")!)
            request.httpMethod = "POST"
            request.timeoutInterval = 300
            request.httpBody = body
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            return (bytes, response as! HTTPURLResponse)
        }
        var (bytes, response) = try await send(try await accessToken())
        if response.statusCode == 401 { (bytes, response) = try await send(try await accessToken(force: true)) }
        if response.statusCode == 400, quick {
            // A model without adjustable reasoning: ask again without it.
            fields["reasoning"] = nil
            body = try JSONSerialization.data(withJSONObject: fields)
            (bytes, response) = try await send(try await accessToken())
        }
        guard response.statusCode == 200 else {
            var data = Data()
            for try await byte in bytes.prefix(4000) { data.append(byte) }
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let code = ((json?["error"] as? [String: Any])?["code"] as? String) ?? (json?["code"] as? String) ?? ""
            throw Self.usageError(code, status: response.statusCode)
        }
        // Server-sent events, one JSON object per "data:" line.
        var text = ""
        var completed = false
        for try await line in bytes.lines where line.hasPrefix("data:") {
            let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
            guard payload != "[DONE]",
                  let event = try? JSONSerialization.jsonObject(with: Data(payload.utf8)) as? [String: Any],
                  let type = event["type"] as? String else { continue }
            switch type {
            case "response.output_text.delta":
                text += event["delta"] as? String ?? ""
            case "response.completed":
                completed = true
            case "response.failed", "error":
                let code = (((event["response"] as? [String: Any])?["error"] as? [String: Any])?["code"] as? String)
                    ?? ((event["error"] as? [String: Any])?["code"] as? String) ?? (event["code"] as? String) ?? ""
                throw Self.usageError(code, status: 0)
            case "response.incomplete":
                throw AIError.failed("The answer was cut short. The file may be too long.")
            default:
                break
            }
        }
        guard completed else { throw AIError.failed("The connection to ChatGPT ended early. Please try again.") }
        return text
    }

    private static func usageError(_ code: String, status: Int) -> AIError {
        if code == "subscription_sharing_usage_limit_exceeded" || status == 429 {
            return .failed("You've reached a ChatGPT usage limit. See chatgpt.com/settings/usage.")
        }
        if code == "subscription_sharing_usage_unavailable" || status == 503 {
            return .failed("ChatGPT is temporarily unavailable. Please try again in a moment.")
        }
        if code == "subscription_sharing_invalid_user" || status == 401 {
            return signedOut("ChatGPT couldn't confirm your plan. Continue with ChatGPT in Settings › AI to reconnect.")
        }
        return .failed("ChatGPT couldn't answer\(status > 0 ? " (\(status))" : ""). Please try again.")
    }

    // MARK: Helpers

    private static func postForm(_ url: String, _ params: [String: String]) async throws -> (Bool, [String: Any]) {
        var request = URLRequest(url: URL(string: url)!)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        request.httpBody = params
            .map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: allowed) ?? "")" }
            .joined(separator: "&").data(using: .utf8)
        let (data, response) = try await URLSession.shared.data(for: request)
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return ((response as? HTTPURLResponse)?.statusCode == 200, json)
    }

    private static func openIdConfig() async throws -> [String: Any] {
        let (data, _) = try await URLSession.shared.data(from: URL(string: "\(issuer)/.well-known/openid-configuration")!)
        guard let config = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw AIError.failed("Couldn't reach OpenAI's sign-in service.")
        }
        // Only trust endpoints on OpenAI's own sign-in host.
        for key in ["jwks_uri", "revocation_endpoint"] {
            guard let value = config[key] as? String else { continue }
            guard let url = URL(string: value), url.scheme == "https", url.host == URL(string: issuer)!.host,
                  url.user == nil else { throw AIError.failed("Unexpected sign-in configuration.") }
        }
        return config
    }

    /// Checks the ID token's RS256 signature against OpenAI's published keys, and its claims.
    private static func verifyIdToken(_ token: String, clientId: String, nonce: String) async throws -> [String: Any] {
        let invalid = AIError.failed("The sign-in response couldn't be verified. Please try again.")
        let parts = token.split(separator: ".").map(String.init)
        guard parts.count == 3,
              let headerData = base64URLDecode(parts[0]), let payloadData = base64URLDecode(parts[1]),
              let signature = base64URLDecode(parts[2]),
              let header = try JSONSerialization.jsonObject(with: headerData) as? [String: Any],
              let payload = try JSONSerialization.jsonObject(with: payloadData) as? [String: Any],
              header["alg"] as? String == "RS256" else { throw invalid }
        let config = try await openIdConfig()
        guard let jwksURL = (config["jwks_uri"] as? String).flatMap(URL.init(string:)) else { throw invalid }
        let (jwksData, _) = try await URLSession.shared.data(from: jwksURL)
        guard let keys = (try JSONSerialization.jsonObject(with: jwksData) as? [String: Any])?["keys"] as? [[String: Any]],
              let jwk = keys.first(where: { $0["kid"] as? String == header["kid"] as? String }),
              let modulus = (jwk["n"] as? String).flatMap(base64URLDecode),
              let exponent = (jwk["e"] as? String).flatMap(base64URLDecode),
              let key = SecKeyCreateWithData(
                rsaPublicKeyDER(modulus: modulus, exponent: exponent) as CFData,
                [kSecAttrKeyType: kSecAttrKeyTypeRSA, kSecAttrKeyClass: kSecAttrKeyClassPublic] as CFDictionary,
                nil
              ),
              SecKeyVerifySignature(key, .rsaSignatureMessagePKCS1v15SHA256,
                                    Data("\(parts[0]).\(parts[1])".utf8) as CFData, signature as CFData, nil)
        else { throw invalid }
        let audience = (payload["aud"] as? [String]) ?? [(payload["aud"] as? String) ?? ""]
        let azp = payload["azp"] as? String
        let authorizedParty = audience.count > 1 ? azp == clientId : (azp == nil || azp == clientId)
        guard payload["iss"] as? String == issuer, audience.contains(clientId), authorizedParty,
              payload["nonce"] as? String == nonce,
              let exp = payload["exp"] as? Double, Date(timeIntervalSince1970: exp) > Date() else { throw invalid }
        return payload
    }

    /// PKCS#1 RSAPublicKey: SEQUENCE { INTEGER modulus, INTEGER exponent }.
    private static func rsaPublicKeyDER(modulus: Data, exponent: Data) -> Data {
        func length(_ count: Int) -> Data {
            if count < 0x80 { return Data([UInt8(count)]) }
            var bytes = withUnsafeBytes(of: count.bigEndian, Array.init).drop { $0 == 0 }
            bytes.insert(0x80 | UInt8(bytes.count), at: bytes.startIndex)
            return Data(bytes)
        }
        func integer(_ value: Data) -> Data {
            var bytes = Data(value.drop { $0 == 0 })
            if let first = bytes.first, first & 0x80 != 0 { bytes.insert(0, at: 0) }
            return Data([0x02]) + length(bytes.count) + bytes
        }
        let body = integer(modulus) + integer(exponent)
        return Data([0x30]) + length(body.count) + body
    }

    private static func randomToken(_ count: Int) -> String {
        var bytes = [UInt8](repeating: 0, count: count)
        _ = SecRandomCopyBytes(kSecRandomDefault, count, &bytes)
        return base64URL(Data(bytes))
    }

    private static func base64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func base64URLDecode(_ text: String) -> Data? {
        var base64 = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        return Data(base64Encoded: base64)
    }
}

/// A one-shot HTTP listener on 127.0.0.1 that receives the browser's sign-in redirect.
/// Only a request carrying this attempt's state counts, so stray local requests can't end the sign-in.
final class LoopbackServer: @unchecked Sendable {
    private let listener: NWListener
    private let queue = DispatchQueue(label: "io.github.sternbj.syncbridge.loopback")
    private let expectedState: String
    private var result: Result<[String: String], Error>?
    private var waiter: CheckedContinuation<[String: String], Error>?
    private(set) var port: UInt16 = 0
    private var listening = false

    private static let page = """
    <!doctype html><meta charset="utf-8"><title>SyncBridge</title>
    <body style="font:16px -apple-system,system-ui;display:grid;place-items:center;height:90vh">
    <p>You can close this tab and return to SyncBridge.</p></body>
    """

    private init(listener: NWListener, expectedState: String) {
        self.listener = listener
        self.expectedState = expectedState
    }

    static func start(preferredPort: UInt16, expectedState: String) async throws -> LoopbackServer {
        do {
            return try await make(port: NWEndpoint.Port(rawValue: preferredPort)!, expectedState: expectedState)
        } catch {
            return try await make(port: .any, expectedState: expectedState)
        }
    }

    private static func make(port: NWEndpoint.Port, expectedState: String) async throws -> LoopbackServer {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: port)
        parameters.allowLocalEndpointReuse = true
        let server = LoopbackServer(listener: try NWListener(using: parameters), expectedState: expectedState)
        try await server.listen()
        return server
    }

    private func listen() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            // Called on `queue` only.
            listener.stateUpdateHandler = { [weak self] state in
                guard let self, !self.listening else { return }
                switch state {
                case .ready:
                    self.listening = true
                    self.port = self.listener.port?.rawValue ?? 0
                    continuation.resume()
                case .failed(let error), .waiting(let error):
                    // .waiting means the port is busy; the caller then tries another one.
                    self.listening = true
                    self.listener.cancel()
                    continuation.resume(throwing: error)
                default:
                    break
                }
            }
            listener.newConnectionHandler = { [weak self] connection in self?.handle(connection) }
            listener.start(queue: queue)
        }
    }

    private func handle(_ connection: NWConnection) {
        connection.start(queue: queue)
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { [weak self] data, _, _, _ in
            guard let self else { return }
            let requestLine = data.flatMap { String(data: $0, encoding: .utf8) }?
                .components(separatedBy: "\r\n").first ?? ""
            let target = requestLine.split(separator: " ").dropFirst().first.map(String.init) ?? ""
            let components = URLComponents(string: "http://127.0.0.1\(target)")
            var query: [String: String] = [:]
            components?.queryItems?.forEach { query[$0.name] = $0.value ?? "" }
            let matches = components?.path == "/auth/callback" && query["state"] == self.expectedState
            let body = matches ? Self.page : "Not found"
            let head = "HTTP/1.1 \(matches ? "200 OK" : "404 Not Found")\r\nContent-Type: text/html; charset=utf-8\r\n"
                + "Cache-Control: no-store\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n"
            connection.send(content: Data((head + body).utf8), completion: .contentProcessed { _ in connection.cancel() })
            if matches { self.finish(.success(query)) }
        }
    }

    private func finish(_ outcome: Result<[String: String], Error>) {
        guard result == nil else { return }
        result = outcome
        if let waiter {
            self.waiter = nil
            waiter.resume(with: outcome)
        }
    }

    func waitForCallback(timeout: TimeInterval) async throws -> [String: String] {
        queue.asyncAfter(deadline: .now() + timeout) { [weak self] in
            self?.finish(.failure(AIError.failed("Sign-in timed out. Please try again.")))
        }
        return try await withCheckedThrowingContinuation { continuation in
            queue.async {
                if let result = self.result { continuation.resume(with: result) } else { self.waiter = continuation }
            }
        }
    }

    func stop() {
        listener.cancel()
        queue.async { self.finish(.failure(AIError.failed("Sign-in was stopped."))) }
    }
}

// MARK: - Claude Code

struct ClaudeCodeStatus {
    var path: String?
    var folder = ""
    var email = ""
    var version = ""
    var loggedIn = false
    var usesPlan = false
    var plan = ""
}

/// Runs the person's own Claude Code CLI, signed in with their Claude plan.
enum ClaudeCode {
    static let models: [(id: String, label: String)] = [("", "Default"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("haiku", "Haiku")]

    /// Claude Code settings folder (CLAUDE_CONFIG_DIR) to use; empty means Claude Code's default
    /// (~/.claude). People with several Claude accounts keep each in its own folder.
    static let configFolderKey = "claudeConfigDir"

    static var configFolder: String {
        get { UserDefaults.standard.string(forKey: configFolderKey) ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: configFolderKey) }
    }

    /// Apps opened from Finder get a minimal PATH, which leaves out where Claude Code
    /// (and Node for npm installs) usually live.
    private static func environment(configFolder: String) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        let extra = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "\(home)/.claude/local"]
        env["PATH"] = ([env["PATH"] ?? "/usr/bin:/bin"] + extra).joined(separator: ":")
        if configFolder.isEmpty {
            env.removeValue(forKey: "CLAUDE_CONFIG_DIR")
        } else {
            env["CLAUDE_CONFIG_DIR"] = configFolder
        }
        return env
    }

    /// ~/.claude and ~/.claude-* folders: candidates for the account picker.
    static func candidateFolders() -> [String] {
        let home = NSHomeDirectory()
        let names = (try? FileManager.default.contentsOfDirectory(atPath: home)) ?? []
        return names.filter { $0 == ".claude" || $0.hasPrefix(".claude-") }.sorted()
            .map { "\(home)/\($0)" }
            .filter { var isDir: ObjCBool = false; return FileManager.default.fileExists(atPath: $0, isDirectory: &isDir) && isDir.boolValue }
    }

    static func find() -> String? {
        let home = NSHomeDirectory()
        let dirs = ["\(home)/.local/bin"]
            + (environment(configFolder: "")["PATH"] ?? "").split(separator: ":").map(String.init)
        return dirs.filter { $0.hasPrefix("/") }.map { "\($0)/claude" }
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// Login state of Claude Code for one settings folder ("" = default).
    static func status(configFolder: String = ClaudeCode.configFolder) async -> ClaudeCodeStatus {
        await Task.detached {
            var status = ClaudeCodeStatus(path: find())
            status.folder = configFolder
            guard let path = status.path else { return status }
            status.version = (try? runSync(path, ["--version"], configFolder: configFolder, timeout: 15))?.stdout
                .split(separator: " ").first.map(String.init) ?? ""
            if let output = try? runSync(path, ["auth", "status"], configFolder: configFolder, timeout: 15)?.stdout,
               let json = try? JSONSerialization.jsonObject(with: Data(output.utf8)) as? [String: Any] {
                status.loggedIn = json["loggedIn"] as? Bool ?? false
                status.usesPlan = json["authMethod"] as? String == "claude.ai"
                status.plan = json["subscriptionType"] as? String ?? ""
                status.email = json["email"] as? String ?? ""
            }
            return status
        }.value
    }

    /// Signed-in accounts among ~/.claude and ~/.claude-* (checked in parallel).
    static func accounts() async -> [ClaudeCodeStatus] {
        await withTaskGroup(of: ClaudeCodeStatus.self) { group in
            for folder in candidateFolders() { group.addTask { await status(configFolder: folder) } }
            var found: [ClaudeCodeStatus] = []
            for await status in group where status.loggedIn { found.append(status) }
            return found.sorted { $0.folder < $1.folder }
        }
    }

    static func complete(system: String, prompt: String, model: String) async throws -> String {
        guard let path = find() else {
            throw AIError.notConfigured("Claude Code isn't installed. See Settings › AI.")
        }
        let configFolder = configFolder
        return try await Task.detached {
            // A private, empty working folder: no project settings, MCP servers, commands or tools.
            let folder = FileManager.default.temporaryDirectory.appendingPathComponent("syncbridge-claude-\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            defer { try? FileManager.default.removeItem(at: folder) }
            let systemFile = folder.appendingPathComponent("system.txt")
            try system.write(to: systemFile, atomically: true, encoding: .utf8)
            var args = ["-p", "--output-format", "json", "--tools", "", "--system-prompt-file", systemFile.path,
                        "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
                        "--no-session-persistence"]
            if models.contains(where: { $0.id == model && !model.isEmpty }) { args += ["--model", model] }
            guard let run = try runSync(path, args, input: prompt, directory: folder,
                                        configFolder: configFolder, timeout: 300) else {
                throw AIError.failed("Claude took too long to answer. Please try again.")
            }
            let json = (try? JSONSerialization.jsonObject(with: Data(run.stdout.utf8))) as? [String: Any]
            if let json, json["is_error"] as? Bool != true, let text = json["result"] as? String {
                return text
            }
            let message = (json?["result"] as? String) ?? run.stderr
            if message.range(of: "log ?in|logged in|authenticat", options: [.regularExpression, .caseInsensitive]) != nil {
                throw AIError.notConfigured("Claude Code isn't signed in. Open Terminal, run `claude`, and sign in with your Claude account.")
            }
            if message.range(of: "limit|usage", options: [.regularExpression, .caseInsensitive]) != nil {
                throw AIError.failed("You've reached your Claude usage limit. \(message.split(separator: "\n").first ?? "")")
            }
            throw AIError.failed(message.split(separator: "\n").first.map(String.init) ?? "Claude Code couldn't answer.")
        }.value
    }

    /// Runs a program and waits; nil when it took longer than `timeout` seconds (it is then stopped).
    private static func runSync(_ path: String, _ args: [String], input: String? = nil, directory: URL? = nil,
                                configFolder: String, timeout: TimeInterval) throws -> (stdout: String, stderr: String)? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = args
        process.environment = environment(configFolder: configFolder)
        process.currentDirectoryURL = directory ?? FileManager.default.temporaryDirectory
        let output = Pipe(), errors = Pipe(), stdin = Pipe()
        process.standardOutput = output
        process.standardError = errors
        process.standardInput = stdin
        try process.run()
        if let input { stdin.fileHandleForWriting.write(Data(input.utf8)) }
        try? stdin.fileHandleForWriting.close()
        var outData = Data(), errData = Data()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global().async { outData = output.fileHandleForReading.readDataToEndOfFile(); group.leave() }
        group.enter()
        DispatchQueue.global().async { errData = errors.fileHandleForReading.readDataToEndOfFile(); group.leave() }
        if group.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            return nil
        }
        process.waitUntilExit()
        return (String(decoding: outData, as: UTF8.self), String(decoding: errData, as: UTF8.self))
    }
}
