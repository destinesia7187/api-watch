# Security model

API Watch accepts user-controlled outbound HTTP configuration. The primary threats are server-side request forgery, credential disclosure, cross-account access, unbounded requests/responses, and repeat execution of operations with side effects.

## Outbound destination policy

Validation runs when saving a configuration and immediately before a check connects. A saved domain can change its DNS records, so save-time validation is not a trust grant.

- Only absolute HTTP and HTTPS URLs are supported. URL credentials, fragments, query strings in the URL field, control characters, whitespace, backslashes, scoped addresses, and ambiguous IP encodings are rejected. Query parameters use a separate ordered list.
- Production targets use ports 80 or 443. The checker rejects non-public addresses, including loopback, private, link-local, metadata, multicast, reserved and IPv4-mapped non-public addresses.
- All returned A/AAAA addresses must pass policy. A mixed public/private answer is blocked rather than selecting only its public member.
- The transport selects one validated address and pins the connection to it. Host, TLS SNI, and certificate hostname remain the original hostname. There is no DNS-resolution gap between validation and connecting.
- Redirects are never followed. An explicitly expected 3xx status can succeed, but its `Location` is not requested.
- Environment proxy settings are ignored. Cookies are neither persisted nor reused. Managed/proxy/hop-by-hop headers cannot be supplied by a user.
- TLS certificate verification stays enabled. Bearer tokens and secret headers require HTTPS.
- `APP_ENV=test` can explicitly allow a named fixture host/port through `TEST_ALLOWED_HOSTS`. Settings reject this override in development and production. This mechanism exists for isolated test servers, never for a public deployment.

An operator should also deny outbound access to internal/service networks, cloud metadata, host/container gateways and infrastructure destinations using a firewall or egress gateway. The default Compose data network isolates the database and does not publish its port, while the checker enforces address policy. Compose is not a complete host firewall. Test the actual deployment's routing and egress rules before making registration public.

A public endpoint can itself proxy traffic elsewhere; API Watch cannot control a remote server's subsequent requests. Protect credentials by monitoring services you own or are authorized to access.

## Request and response bounds

The own API accepts at most 256 KiB per request, including streaming/chunked bodies. Monitor requests have 20 total headers, a 16 KiB combined header bound, up to 100 ordered query pairs with a 16 KiB combined bound, and a 64 KiB JSON body bound. Check intervals are 60 seconds to 24 hours and total request timeouts are 1–30 seconds.

The target HTTP deadline includes DNS, TCP, TLS, send and read. Waiting for API Watch's local destination capacity is excluded from target timeout/latency; the outer worker lease still bounds that wait. Both received and decompressed response data are limited to 1 MiB. Unsupported content encodings fail. JSON parsing rejects duplicate object keys, non-finite numbers, malformed JSON, and nesting deeper than 64 levels. History stores safe assertion indexes, never actual or expected secret values.

Deployment-wide admission defaults to 10 checks, with at most two active connections per chosen IP. Persistent API rate limits are shared across processes. Changing worker count does not multiply these limits.

## Secrets and retained data

Request configuration, target tokens/secret headers, Telegram chat identifiers, recipient snapshots, and password-reset delivery payloads use AES-GCM authenticated encryption. The random nonce is stored with the ciphertext; `ENCRYPTION_KEY_ID` is authenticated as additional data. The encryption key is supplied separately from the database.

Passwords use Argon2id with a unique salt (19 MiB memory, two iterations and parallelism one). Session identifiers, personal API tokens, password-reset tokens and Telegram link codes are stored as hashes. Tokens are generated from cryptographically secure randomness.

The API displays a new personal API token once and returns only metadata thereafter. Target Bearer tokens and secret headers are never returned after creation; their configured state is returned instead. Ordinary headers, query parameters and request bodies are configuration data visible to their owner. Put credentials in Bearer authentication or secret headers, rather than ordinary configuration fields.

Stored history excludes response bodies, raw request/response headers, outgoing request bodies, and target credentials. Errors use fixed safe messages; exceptions and Telegram descriptions are not reflected to clients. Bot API URLs contain a token, so HTTP access/client debug logging must stay disabled for them.

The `.env`, development keys, local mail, database files and generated test artifacts are ignored by Git. Development can persist generated keys under ignored `var/`; production refuses missing/short secrets. Back up keys separately with restricted permissions. Changing only the environment key makes existing data unreadable; use the offline rotation procedure in [operations.md](operations.md).

Deleting a monitor immediately cancels its active leases and erases all encrypted request payloads. Version metadata and retained safe history remain until retention cleanup. Deleting an account removes owned monitoring data, sessions, tokens, links and delivery rows.

## Authentication and browser protection

Browser login creates a new random server-side session; only its hash is stored. Cookies use `HttpOnly`, `SameSite=Lax`, a root path and `Secure` in production. Absolute expiry is seven days; idle expiry is 24 hours. Login replaces a previous session cookie. Logout revokes the session.

State-changing authenticated browser requests require a session-bound `X-CSRF-Token` and an allowed Origin when Origin is present. Authentication entry points also enforce Origin. There is no permissive credentialed CORS policy. The application and API are served from the same configured origin.

Personal API tokens have `read` or `read_write` scope, expire in 1–365 days, and are limited to five active tokens per user. Administrative account/token/integration operations require a browser session. Supplying both a cookie and Bearer token is rejected to avoid ambiguous identity.

Every user resource is constrained by ownership, including nested checks, incidents, notifications, statistics, pagination cursors and API tokens. A foreign or deleted resource returns `404`. Signed pagination cursors are bound to the owner, route/filter context and stable creation-time ordering.

Production requires HTTPS, PostgreSQL, generated secrets, and SMTP for account recovery. Responses set no-sniff, no-referrer, anti-framing and a same-origin content security policy; production also sets HSTS. The interactive Swagger page is the specific CDN-script exception to the dashboard's CSP.

Password resets have generic public responses, one-use tokens and a 30-minute expiry. Changing/resetting a password invalidates existing sessions and API tokens. Account deletion requires the current password. Local development reset messages are written into ignored `var/mail`; production never falls back to local files.

## Telegram trust boundary

The server contacts only the fixed HTTPS Bot API origin, with redirects disabled. The user cannot submit an arbitrary `chat_id`. A browser session creates a 10-minute one-use deep link; the poller checks a private chat and consumes its hashed code transactionally.

Connection generations fence old outbox rows after disconnect/relink. Notifications contain the monitor name, hostname without query, incident ID, UTC event time and safe reason/duration. Messages have no parse mode. A message already in flight cannot always be recalled, and an uncertain network retry can duplicate a Telegram message.

## Verification and reporting

Tests exercise private/mapped/encoded addresses, mixed DNS answers, DNS pinning, redirects, TLS/network errors, response bounds, JSON semantics, secret masking, CSRF, scopes and cross-account resource access. The PostgreSQL suite exercises admission/locking guarantees when `TEST_DATABASE_URL` is provided. CI and test commands are documented in the README.

Do not publish credentials in an issue. For a suspected vulnerability, include a minimal redacted reproduction and the affected commit. This repository is a portfolio application; a test run is evidence of the scenarios covered, not a claim of an independent security audit.
