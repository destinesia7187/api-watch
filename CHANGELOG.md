# Changelog

## 1.0.0 — 2026-10-19

First complete release of API Watch.

- Account registration, browser sessions, scoped personal API tokens, password recovery/change, and account deletion.
- Private HTTP/HTTPS monitors with immutable encrypted request configurations, Bearer tokens, secret headers, repeated query parameters, JSON bodies and JSON Pointer assertions.
- Durable background scheduling, bounded outbound HTTP, DNS-pinned SSRF protection, PostgreSQL admission and fencing, missed-slot accounting, and manual diagnostics with idempotency keys.
- Confirmed incidents, recovery and abort transitions, encrypted Telegram linking, and a transactional notification outbox with bounded retries and delivery history.
- English dashboard with check/incident filters, response-time and successful-check charts, explicit missing observations, and configuration-version context.
- Checkly-inspired navy visual system with horizontal top navigation, inline summary metrics, an expanding mobile header row, and self-hosted Inter and JetBrains Mono fonts.
- OpenAPI contracts, Alembic migrations, locked dependencies, Docker Compose, GitHub Actions, automated security/concurrency tests, and reproducible performance/recovery tools.

Operational configuration requires the deployment owner's Telegram/SMTP credentials, production origin, encryption keys and network policy. Recorded verification scope and measurements are documented in `docs/acceptance.md` and `docs/performance.md`.
