# API Watch — Technical Specification

**Project:** website and REST API availability and response-time monitoring.

**Release:** 1.0. **Date:** October 19, 2026.

**Purpose:** a complete GitHub portfolio application with an English dashboard, its own documented REST API, reproducible deployment, and automated verification.

**Status:** release behavior and acceptance requirements. Actual test, performance, and recovery evidence must be read separately; a requirement is not itself proof of acceptance.

## 1. Objective

Automatically send configured HTTP requests, evaluate status/latency/JSON conditions, retain safe results, detect persistent failures, and notify owners about opening, recovery, and unconfirmed termination of incidents. Users manage private monitors through the dashboard or scoped personal API tokens.

Metrics describe sampled observations rather than exact physical uptime. The service does not perform load testing or automatically replay uncertain state-changing HTTP operations.

## 2. Release scope

The first release includes accounts, expiring sessions, password change/reset, account deletion, ownership isolation, personal API tokens, full monitor lifecycle, HTTP request configuration and target credentials, JSON assertions, durable scheduling and leases, global/per-IP admission, safe history, statistics/charts, incidents, Telegram linking/outbox/retries/delivery status, a responsive dashboard, OpenAPI, migrations, Compose, CI, tests, retention, security, and operations/recovery documentation.

Teams, public status pages, billing, more notification channels, and a separate broker are future additions. They do not substitute for the requirements listed here.

## 3. Terms and actors

| Term | Meaning |
| --- | --- |
| User | Owner of private monitors and associated data |
| Monitor | Request configuration, schedule, owner, execution state and observed health |
| Configuration version | Immutable encrypted request snapshot plus safe timing/threshold metadata |
| Run/check | One logical scheduled or manual operation |
| Slot | Anchored scheduled due time |
| Gap | Aggregated missed-slot range and count, not a single HTTP result |
| Worker | Process performing admitted checks outside database transactions |
| Incident | Confirmed failure observation interval |
| Outbox | Durable event persisted with an incident transition |
| Lease | Random fencing token and finite database-clock deadline |

Ordinary users cannot access other accounts' monitors, nested checks, incidents, statistics, tokens, or deliveries. Operators administer infrastructure through operational tools; there is no user-facing administrator role.

## 4. User scenarios

Register and create a first monitor; observe a public website; monitor an authenticated HTTPS API; assert JSON fields; inspect safe errors; run an independent manual diagnostic; pause/edit/resume with version history; receive confirmed incident/recovery/abort events; filter history/charts; use a personal token; recover credentials/delete an account; launch from a clean clone and reproduce a controlled failure/recovery demonstration.

## 5. Accounts and authorization

**AUTH-01.** Normalize email by trimming/lowercasing and enforce uniqueness. Registration creates an account without automatic login and can be disabled by the operator.

**AUTH-02.** Passwords contain 12–128 characters and at most 512 UTF-8 bytes. Store salted Argon2id hashes; initial parameters are 19 MiB memory, two iterations and parallelism one. Never retain or return plaintext passwords.

**AUTH-03.** Browser session identifiers contain at least 32 random bytes and are stored only as hashes. Login rotates the session. Absolute lifetime is seven days; idle lifetime is 24 hours. Logout revokes it. Cookies use HttpOnly, SameSite=Lax, root path, and Secure in production.

**AUTH-04.** Authenticated browser mutations require a session-bound X-CSRF-Token and an allowed Origin when supplied. Authentication entry points also enforce Origin. Dashboard and API share the configured origin; permissive credentialed CORS is prohibited.

**AUTH-05.** Allow five active personal API tokens per account: read or read_write scope, 1–365 days, default 90. Show the secret once; retain its hash and prefix. Revocation takes effect immediately. Read tokens cannot modify resources. Reject simultaneous cookie and Bearer identity.

**AUTH-06.** Account, token, and integration administration requires a browser session. Monitoring/history/statistics accepts a session or appropriately scoped token. Every resource access checks ownership; missing, foreign, and deleted resources return 404.

**AUTH-07.** Password-reset initiation returns a generic accepted response. A random hashed one-use reset token expires after 30 minutes. Send mail through an encrypted outbox and SMTP; ignored local development mail is permitted. Reset/change revokes sessions and personal API tokens. Account deletion verifies the current password, cancels active checks and removes owned data/secrets through foreign-key cascades.

## 6. Monitor configuration

| Field | Contract |
| --- | --- |
| name / description | Trimmed 1–100 / up to 1,000 characters |
| url | Absolute normalized HTTP/HTTPS, up to 2,048 characters; no URL credentials/query/fragment |
| method | GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS |
| headers / secret_headers | Ordinary pairs / encrypted write-only credential pairs |
| query_params | Ordered pairs preserving repeated names and empty values |
| body | enabled flag and explicit JSON value, including null |
| auth | none or bearer; token is write-only |
| side_effects_acknowledged | Required true for POST/PUT/PATCH/DELETE |
| interval_seconds | Integer 60–86,400; default 60 |
| timeout_ms | Integer 1,000–30,000; default 10,000; less than interval |
| expected_status | Integer 200–599; default 200 |
| max_response_time_ms | Optional positive completed-response threshold no greater than timeout |
| json_assertions | Up to 20 JSON Pointer conditions combined with AND |
| failure_threshold / recovery_threshold | Integer 1–10; defaults 3/2 |
| notifications_enabled | Enable incident transition notifications |
| enabled | Initial active/paused choice; later use pause/resume |

Allow 20 total headers with unique case-insensitive ASCII names, 4 KiB value bounds, and a 16 KiB combined bound. Reject control/CRLF injection and transport-managed Host, Content-Length, Content-Type, Accept-Encoding, Cookie, hop-by-hop, proxy, and forwarding headers. Authorization is a secret header or Bearer auth, never an ordinary header; reject duplicate authorization sources.

Allow 100 ordered query pairs, 16 KiB combined, with names up to 1,000 bytes and values up to 4,096 bytes. Outgoing JSON is limited to 64 KiB and 64 nesting levels. GET/HEAD/OPTIONS have no body; HEAD has no JSON assertions. An enabled body requires an explicit json value; disabled and JSON null differ.

Credentials require HTTPS and authenticated encryption. Reads return only auth type/configured and secret header names/configured. Ordinary headers, query/body, and assertion expectations are owner-visible configuration; credentials belong in dedicated secret fields.

## 7. Lifecycle and concurrent edits

Execution state is active, paused, or deleted. Creation/resume schedules immediately using database time. record_version protects concurrent API changes: require expected_version; reject stale values with 409 VERSION_CONFLICT. config_version changes only for effective execution-setting changes, not metadata/notification switches.

Execution edits require an idle paused monitor; otherwise return 409 MONITOR_MUST_BE_IDLE_AND_PAUSED. Validate the complete merged configuration atomically; write an immutable encrypted version, reset health/streaks/watermark, and abort an open incident with configuration_changed.

PATCH omission preserves a field. Ordinary header/query/assertion arrays and body replace as a whole. Secret-header operations use case-insensitive {name,value} to add/replace or {name,remove:true} to remove; omitted names persist, and an empty operation list changes nothing. Reject duplicate/invalid operations. auth.type=none removes Bearer; bearer without token preserves only an existing Bearer. Mask strings have no preservation meaning.

Pause prevents new scheduling, clears due time, terminates queued/running checks as cancelled, invalidates leases, and resets streaks/health to unknown. Request prompt local HTTP cancellation; a request already received remotely cannot be recalled. Pause does not resolve an incident. Resume retains incidents and schedules immediately with reset streaks/unknown health. Repeating an already reached state with the current version does not shift scheduling again.

Deletion hides the monitor and nested resources, stops scheduling, cancels active tasks, aborts with monitor_deleted, and immediately erases encrypted request payloads for all versions. Keep safe history/version metadata only under retention. Undelete is outside release 1.0.

## 8. HTTP and JSON evaluation

Issue at most one HTTP request per run. Disable automatic reconnection retries, redirects, environment proxies, and cookie reuse. Do not retry 429, timeouts or connect failures inside a check; the target may already have applied an operation.

One target timeout covers DNS, TCP/TLS, send and read. Waiting for API Watch's own destination capacity is excluded from this target budget and from response_time_ms, while the separate worker lease still bounds total local execution. Monotonic response_time_ms covers the exchange through receiving a complete bounded body (headers for HEAD), excluding database/capacity queues and JSON evaluation. Incomplete exchanges have null response time and separate elapsed_ms. Fully received unexpected HTTP statuses retain response time.

Send Accept-Encoding: identity. Stream and bound encoded and decompressed responses to 1 MiB each. Reject unsupported encodings and excessive header fields/lines. Verify TLS against the original hostname.

Use RFC 6901 JSON Pointer: empty root, /items/0/id array navigation, ~1 for slash, ~0 for tilde. Reject invalid escapes, fragment form and wildcards. Missing differs from present null. exists accepts no expected field; equals requires explicit expected, including null. Conditions are ANDed.

Equality is typed: booleans differ from numbers, strings differ from numbers, arrays are ordered, and object member order is irrelevant. Use exact decimal numeric evaluation; numeric 1 and 1.0 are equal. Preserve exact configured numeric literals across storage/API boundaries. Reject malformed JSON, duplicate keys, nonfinite numbers and depth beyond 64.

Retain failed assertion indexes, never actual response values. Primary failure priority: destination security, network/size, HTTP status, response threshold, JSON parsing, assertions.

## 9. Result model and error codes

Run status is queued, running, or terminal finished. Nonterminal outcomes are null; terminal results apply once.

| Outcome | Meaning | Scheduled success denominator |
| --- | --- | --- |
| success | All configured conditions pass | Included |
| failure | Observable network/target/condition failure | Included |
| blocked | Outbound security policy rejects target | Excluded |
| interrupted | Worker/service problem or uncertain execution | Excluded |
| cancelled | Pause/delete/control action | Excluded |
| skipped | No request due to busy/expired opportunity | Excluded |

Failure codes: dns_error, connection_error, tls_error, timeout, response_too_large, invalid_http_response, unexpected_status, response_too_slow, invalid_json, assertion_failed. Security uses blocked/target_blocked. Service execution uses interrupted/worker_lost or internal_error. Control actions use cancelled/monitor_paused, monitor_deleted or configuration_changed. Individual skips use monitor_busy/queue_expired; aggregate gaps use schedule_missed.

Retain IDs, version/trigger, immutable creation time, slot/start/finish, status/outcome, known HTTP status, response/elapsed durations, bytes, safe error and failed indexes. Do not retain complete response bodies, raw headers, credentials or outgoing bodies in history. A network error may originate in API Watch's infrastructure; not every shared resolver/network outage can be classified automatically.

## 10. Durable scheduling and leases

PostgreSQL stores queue, schedules and outbox. Poll at least every second. Creating a run/gap and advancing next_due_at is one transaction. Row locks/SKIP LOCKED and partial unique indexes permit one queued/running task per monitor, including manual work. Enforce unique monitor/slot pairs.

Schedule from prior due times, not completion. For delay below one interval keep the original slot. Aggregate older missed slots and enqueue only the newest current slot. Do not double count a slot in a gap and individual skip. A busy current slot becomes skipped/monitor_busy while its grid advances.

Scheduled queue expiry is slot plus interval; manual expiry is creation plus 60 seconds. Expired queues become skipped/queue_expired. Claims use short transactions without network I/O. A PostgreSQL admission lock enforces the deployment-wide running cap across workers, default 10. Persist per-chosen-IP destination leases, allowing two live connections per checked IP.

Lease token is random; deadline is timeout_ms plus 15 seconds and cannot extend indefinitely. Completion locks monitor/run and verifies running, token, strictly future database-clock expiry, active state and current configuration. Obtain actual database time after locks; transaction-start timestamps must not weaken fencing.

Expired running work becomes interrupted/worker_lost and is never requeued. Reject late results even before the reaper updates status. Database application is idempotent; exactly-once HTTP and zero physical overlap after worker loss are not promised. Shutdown stops admission and finishes already admitted work within deadlines. Database admission failure prevents new HTTP starts.

## 11. Manual diagnostics

run-now requires an active idle monitor and an 8–128 character Idempotency-Key. Return 202, check_id and Location. Paused/busy monitors return 409. Scope keys by owner/monitor/route for 24 hours; a repeat returns the original check before current busy/paused validation. Only no body or an empty object is accepted; unrelated payloads are rejected. The key never authorizes access.

Manual checks remain history but do not change schedule, health, streaks, incidents or scheduled statistics. A scheduled slot occupied by manual work is explicitly skipped.

## 12. Health and incidents

| Health | Meaning |
| --- | --- |
| unknown | No current evaluable scheduled observation; an incident can remain open |
| up | Successful latest observation and no open incident |
| degraded | Failure streak below opening threshold |
| down | Open incident and latest evaluable failure |
| recovering | Open incident and success streak below recovery threshold |

Only scheduled success/failure changes streaks. Failure resets recovery; success resets failure. Non-evaluable scheduled outcomes and gaps reset both and set unknown without resolving. Manual diagnostics neither change nor break streaks.

Order by scheduled slot using last_applied_scheduled_at for the current configuration. Only strictly newer slots apply; gaps use the last missed slot. Older late results remain history/statistics without reverting health.

At failure_threshold open one incident: started_at is first failure completion; detected_at is confirming failure completion. Save config version, first/confirming/last run IDs and safe reason. Further failures update the same incident; a partial unique index enforces one open incident.

At recovery_threshold resolve: recovery_started_at is the first success of the final uninterrupted success series; resolved_at/ended_at is confirmation. Failure/non-evaluable observations reset pending recovery. Duration includes observation/confirmation delay, not exact physical outage.

Statuses are open, resolved, aborted. Config change/deletion aborts with ended_at and its reason, never false recovery. No manual recovered closure. Result, health/incident and outbox share one transaction. For thresholds 3/2: F,F,S opens none; F,F,F opens one; more F keeps it; S recovers partially; F resets; S,S resolves; F,interrupted,F is two separate one-failure streaks.

## 13. Telegram and durable delivery

One account connects one private chat through a random one-use deep link valid ten minutes. Store a hash and owner; issuing another code revokes prior codes. Accept /start only from the same confirmed private sender/chat. Reject groups, expired/replayed codes and arbitrary submitted chat IDs. Encrypt chat identifiers.

One logical getUpdates poller uses only the fixed HTTPS Bot API. Commit update offset with code consumption so replay cannot relink. Connection generations fence old unsent recipients after disconnect/relink; queued recipients do not change retroactively.

Events: incident_opened, incident_resolved, incident_aborted and explicit test_notification. With monitor alerts off, create no incident event. With alerts on but unlinked, save suppressed/integration_not_connected. Connecting later does not replay these. Disabling monitor alerts cancels unsent events while checks/incidents continue.

Text contains monitor name, hostname without query, incident ID, event, UTC time, safe reason/duration. No credentials/headers/body/JSON values or parse mode. Outbox uniqueness is incident/event type; test events have separate UUIDs.

Preserve per-monitor order: later events wait for predecessors to become sent/failed/suppressed/cancelled. Dispatch uses finite leases and fenced acknowledgements. Permit six attempts: 30/60/120/240/480 second backoffs with ±10% jitter for network/5xx; honor larger valid 429 retry_after. Permanent errors such as 403 terminate failed. Show safe delivery status without blocking checks. Uncertain retries can duplicate external messages; already sending messages cannot always be recalled.

## 14. History, statistics and charts

Use timezone-aware ISO 8601 input, UTC storage, half-open [from,to) periods, default 24 hours, maximum 30 days and available retention bounds. Return 422 plus available range for invalid/out-of-retention periods. UI presets are one hour, 24 hours, seven days and 30 days.

Scheduled history uses started_at, falling back to scheduled_at when unstarted. Manual checks remain history but are excluded from scheduled metrics. Return represented config versions and allow version filtering.

Success percentage is success/(success+failure)*100, null with no evaluated samples. Count blocked/interrupted/cancelled/skipped and aggregate missed slots separately. Clip gap counts accurately. Missing observations are not successes.

Latency samples are successful scheduled complete responses. Return count, arithmetic mean, min, max, median p50 and nearest-rank p95 (ceil(0.95*N), one-based). Empty samples give null. Compute period percentiles from samples rather than averaging bucket percentiles.

Series supports 1m/5m/1h/1d UTC-aligned buckets clipped to the period. Keep empty buckets with zero counts/null metrics. Maximum 1,000 points; automatically choose a suitable bucket or reject an overfine explicit choice with a recommendation.

## 15. Dashboard

Provide English registration/login/reset, monitor list/detail/editor, filtered history, charts, incident views, Telegram/delivery settings and account/token management. Empty states explain first-monitor setup. Validation errors remain actionable and preserve nonsensitive input.

The editor exposes every request/timing/assertion field and explicit side-effect acknowledgement. Existing secrets show configured state. Explain pause-before-execution-edit. Separate execution state from health and manual diagnostics from scheduled observations. Show last-check age/time, status, latency, reason, open incident and busy state.

Confirm destructive operations. Support 360 px width, keyboard use, labels, focus states and textual status. Do not put credentials in localStorage or ordinary application URLs. Reset/deep-link tokens are transient one-use flows and removed when consumed.

## 16. REST API

Prefix application routes /api/v1. Serve /openapi.json and /docs, and export a matching schema. Document identity/CSRF/scopes/constraints/write-only fields/errors. Lists default to 20, range 1–100, stable created_at DESC/id DESC, immutable creation time and signed owner/filter-bound cursors. Invalid cursor gives 400 INVALID_CURSOR. Return items, next_cursor, has_more; cross-page snapshots are not promised.

| Method | Route after /api/v1 | Success |
| --- | --- | --- |
| POST | /auth/register | 201 |
| POST | /auth/login | 200 |
| GET | /auth/me, /auth/csrf | 200 |
| POST | /auth/logout | 204 |
| POST | /auth/password/forgot | 202 |
| POST | /auth/password/reset, /auth/password | 200 |
| DELETE | /auth/account | 204 |
| GET/POST | /api-tokens | 200/201 |
| DELETE | /api-tokens/{id} | 204 |
| GET/POST | /monitors | 200/201 |
| GET/PATCH/DELETE | /monitors/{id} | 200/200/204 |
| POST | /monitors/{id}/pause, /monitors/{id}/resume | 200 |
| POST | /monitors/{id}/run-now | 202 |
| GET | /monitors/{id}/checks, /checks/{id} | 200 |
| GET | /incidents, /incidents/{id} | 200 |
| GET | /monitors/{id}/stats, /monitors/{id}/stats/series | 200 |
| GET/DELETE | /integrations/telegram | 200/204 |
| POST | /integrations/telegram/link | 201 |
| POST | /integrations/telegram/test | 202 |
| GET | /notifications, /notifications/{id}, /service-status | 200 |

Creation returns monitor Location; manual runs/test notifications return ID and status-resource Location. Delete takes expected_version in query; PATCH/pause/resume take it in JSON. Incident period filters select interval overlap, including earlier open incidents; end is resolved_at, aborted ended_at, or infinity.

Example creation (illustrative hostname):

```json
{
  "name": "Catalog API",
  "description": "Catalog health endpoint",
  "url": "https://api.example.com/health",
  "method": "GET",
  "headers": [{"name": "Accept", "value": "application/json"}],
  "secret_headers": [],
  "query_params": [{"name": "details", "value": "true"}],
  "body": {"enabled": false},
  "auth": {"type": "bearer", "token": "replace-with-target-token"},
  "interval_seconds": 60,
  "timeout_ms": 10000,
  "expected_status": 200,
  "max_response_time_ms": 2000,
  "json_assertions": [{"pointer": "/status", "operator": "equals", "expected": "ok"}],
  "failure_threshold": 3,
  "recovery_threshold": 2,
  "notifications_enabled": false,
  "enabled": true
}
```

## 17. Safe API errors

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "The request did not pass validation.",
    "details": [{"field": "body.interval_seconds", "code": "greater_than_equal", "message": "Invalid value."}],
    "request_id": "33333333-3333-4333-8333-333333333333"
  }
}
```

400: malformed JSON/cursor/ambiguous identity; 401: invalid credentials; 403: scopes/session/CSRF/origin/policy; 404: missing/foreign/deleted; 409: version/state/busy/email conflicts; 413: excessive input; 422: invalid fields/target/period/bucket; 429: persistent throttling with Retry-After; 500: safe unexpected error; 503: unavailable dependency/integration configuration.

A GET for a failed target still returns 200 containing failure outcome. Never echo passwords, target tokens, response values or stack traces in validation/internal errors.

## 18. Data and transactions

Entities: users, sessions, api_tokens, password_resets, monitors, monitor_configs, check_runs, schedule_gaps, incidents, destination_leases, telegram_connections, telegram_links, telegram_poll_state, notification_outbox, email_outbox, idempotency_keys, rate_buckets, service_heartbeats.

Use foreign keys, explicit deletion semantics, indexes for owner/queue/history/due/expiry/delivery, and database constraints for identities, versions, active runs and open incidents. Old incident run IDs may remain historical identifiers after detailed retention; safe incident descriptions remain self-contained.

Lock monitor before run. State/outbox changes have explicit transaction boundaries. Network operations never retain check-state transactions. PostgreSQL, rather than in-memory locks alone, coordinates production process-wide admission and limits.

## 19. Security

Validate URL/DNS at save and connect time. Reject private/loopback/link-local/metadata/reserved/multicast/scoped/mapped non-public IPv4/IPv6, mixed DNS, ambiguous encodings, and nonstandard production ports. Pin the selected checked IP while preserving Host/SNI/TLS identity. Disable redirects/proxies. Only APP_ENV=test permits an explicit isolated fixture allowlist.

AES-GCM encrypts config/tokens/chat IDs/recipient/reset-mail snapshots with random nonce and separately stored key/ID. Credentials are write-only; errors/logs are fixed safe messages. Do not log bot URLs/client debug data. Deleted payloads are immediately purged. Document offline all-payload rotation/backup pairing; no online keyring is claimed.

Use HTTPS, secure production cookies, CSRF/same-origin, persistent throttles, ownership filters, CSP/no-sniff/no-referrer/anti-framing/HSTS where appropriate. Document Swagger's asset exception. Supplement application SSRF policy with actual egress restrictions; never publish PostgreSQL publicly.

## 20. Limits and retention

| Setting | Default |
| --- | --- |
| Monitors/account including paused | 20 |
| Active personal tokens/account | 5 |
| Deployment / chosen-IP checks | 10 / 2 |
| Manual checks/account / monitor | 5 per minute / 1 per 10 seconds |
| Login/IP and email separately | 10 per 15 minutes |
| Registration/IP | 5 per hour |
| Authenticated requests/account across identities | 120 per minute |
| Statistics within general limit | 20 per minute |
| Telegram test/account | 3 per 10 minutes |
| Own input / outgoing JSON / incoming encoded and decoded | 256 KiB / 64 KiB / 1 MiB each |
| Finished check/gap detail | 30 days |
| Ended incidents | 180 days after ending |
| Terminal delivery history | 30 days after terminal state |
| Expired idempotency/link/reset cleanup | Normal bounded batches, at least daily |
| Deleted monitor final purge | 180 days after deletion |

Cleanup is bounded and runs at least daily (implementation every minute). Retain open incidents regardless of age. Remove old configuration snapshots only when not current/needed by retained checks/incidents. Purge deleted request secrets immediately. Operators may lower limits; increases require measurements. At 100 minute-interval monitors, history can grow by 144,000 results/day or 4.32 million/30 days.

## 21. Environment, operations and recovery

Use Python 3.12+, FastAPI, SQLAlchemy/Alembic, aiohttp, PostgreSQL, AES-GCM/Argon2 and a same-origin static frontend. Commit exact dependency locks. Compose provides database, explicit migration, web, worker and background services with Windows Docker Desktop/Linux instructions.

.env.example contains no usable secrets. Settings include environment, origin, database, session secret, encryption key/ID, registration/concurrency/logging, optional Telegram and SMTP. Production requires HTTPS/PostgreSQL/generated keys/SMTP. Missing Telegram disables that integration only. No automatically hosted remote site is implied.

Liveness checks web; readiness checks database/schema; authenticated status reports worker/scheduler/notifier/poller heartbeats. Inspect queue depth/age, start delay, running/expired leases, outcomes, database timings and delivery backlog. Web availability does not prove workers are healthy.

Document database-plus-key backup, disposable restore drill, offline rotation, retention, supervision and diagnosis. Actually verify restoration and record evidence. SQLite supports local evaluation/single-process coordination; production/multi-process guarantees require PostgreSQL.

## 22. Performance acceptance

Document a machine of at least 2 vCPU, 4 GiB RAM and SSD. Run 100 monitors at 60-second intervals, controlled responses up to one second and ten admitted checks. Spread slots; report initial mass-start separately. After two-minute warmup observe at least 30 minutes with five users each making one ordinary API request/second within limits.

Targets: scheduled-to-start p95 ≤5 seconds; list/history p95 ≤500 ms; 24-hour stats p95 ≤1 second; 30-day stats p95 ≤2 seconds on representative history. Record hardware/processes/IP contention/data/sample sizes/method. Short smoke runs do not establish full-duration capacity. Publish measurements separately from unverified goals in docs/performance.md.

## 23. Tests and acceptance

Use controlled HTTP/DNS fixtures, substituted Telegram and clocks; core tests need no real site/chat credentials. Real PostgreSQL verifies transactions/concurrency. Skipped environment-dependent tests remain unverified. CI runs lint, migrations, unit/integration and a minimal browser flow.

| ID | Scenario | Required result |
| --- | --- | --- |
| AT-01 | Account/session lifecycle | Correct access, cookies, hashes, revocation |
| AT-02 | Two-account nested access | Foreign resources return 404 |
| AT-03 | CSRF/untrusted origin | Rejected auth/mutation |
| AT-04 | API scope/expiry/revoke | One-time secret and effective permissions |
| AT-05 | Methods/query/body/null | Exact valid outbound operation |
| AT-06 | Target credentials | Target-only transmission, safe reads/history/logs |
| AT-07 | Expected/unexpected status | Correct status/outcome |
| AT-08 | DNS/connect/TLS/timeout | Specific safe codes and metrics |
| AT-09 | Complete slow response | Distinct from timeout |
| AT-10 | Pointer/arrays/escape/null/types/decimals | Exact JSON semantics |
| AT-11 | Invalid/duplicate/deep/oversized/compressed | Bounded failure |
| AT-12 | Internal/mapped/metadata/ambiguous IP | No connection |
| AT-13 | Rebinding/mixed DNS | Only pinned validated address |
| AT-14 | Internal redirect | No second request |
| AT-15 | Concurrent scheduling/workers | One active logical run and application |
| AT-16 | Worker loss after send | Interrupted, never replayed, late result fenced |
| AT-17 | Scheduler outage | Exact gaps, no catch-up storm |
| AT-18 | Pause/resume/delete busy work | Correct cancellation/schedule/incidents |
| AT-19 | Config edit/version conflict | Idle-paused rule, immutable versions |
| AT-20 | Thresholds 3/2 | One incident, correct times/events |
| AT-21 | Non-evaluable/manual/late slot | Correct streaks/watermark |
| AT-22 | Change/delete open incident | Aborted without false recovery |
| AT-23 | Idempotency after finish/pause | Original check during key lifetime |
| AT-24 | Empty/timeout/manual-only stats | Correct null/count/denominator |
| AT-25 | Period/buckets/p95/versions | Reproducible UTC metrics |
| AT-26 | Link expiry/replay/forgery/group | Confirmed one-use private chat |
| AT-27 | Telegram 429/5xx/403/restart/disconnect | Bounded retry/order/generation fencing |
| AT-28 | Concurrent completion/transaction failure | No partial result/incident/outbox |
| AT-29 | Retention/open/deleted | Correct cleanup and immediate secret purge |
| AT-30 | Multiple processes | Deployment/user limits remain shared |
| AT-31 | Desktop/360 px flow | Labeled, readable, keyboard usable |
| AT-32 | Clone/OpenAPI/examples | Reproducible current contract |
| AT-33 | Full workload | Actual targets measured or limitation reported |
| AT-34 | Database/key restore | History/config/secrets recover |
| AT-35 | Password change/reset/replay | One-use expiry and credential revocation |
| AT-36 | Account deletion | Active requests fenced, owned data removed |

## 24. Demonstration and completion

Start from README; register; link a test private chat; create a controlled 200 {"status":"ok"} target; inspect history/chart; switch to 503 until three failures open one incident; restore 200 until two successes resolve it; show deliveries. Demonstrate JSON mismatch, timeout, internal-address blocking, second-account isolation and personal-token reads.

An accelerated demo profile must be explicit and isolated, unavailable to ordinary production users. Do not weaken production timing/security silently.

Deliver source/static assets, migrations, fixtures/tests, locks, Compose/Docker, CI, .env.example, README, OpenAPI/examples and architecture/security/operations/performance documents. Include screenshots/recording only when actually produced. License selection belongs to the repository owner before publishing.

Completion requires working behavior, no known SSRF/ownership flaw, passing clean-environment checks, safe secrets, reproducible migration/start, no uncertain HTTP replay, and honest performance/recovery evidence. Documentation alone does not complete an implementation or acceptance exercise.

## 25. References

Protocol/security references inform the design; numerical limits and incident policy are project decisions.

- [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
- [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)
- [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901.html)
- [Telegram Bot API](https://core.telegram.org/bots/api)
