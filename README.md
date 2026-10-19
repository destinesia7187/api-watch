# API Watch

**Know when your websites and APIs fail — and when they recover.**

API Watch is a self-hosted monitoring application with a web dashboard, a documented REST API, persistent background checks, incident confirmation, and Telegram alerts.

The project focuses on the parts that make a monitoring service trustworthy: bounded HTTP requests, DNS-pinned SSRF protection, user isolation, encrypted credentials, durable scheduling, and explicit treatment of missing observations.

## Features

- Accounts, revocable sessions, CSRF protection, password recovery, and account deletion.
- Personal API tokens with read-only or read/write access.
- HTTP methods, repeated query parameters, public/secret headers, JSON request bodies, and Bearer authentication.
- Expected HTTP status, timeout, response-time threshold, and typed JSON Pointer assertions.
- Scheduled and manual diagnostic checks, check history, pagination, and configuration versions.
- Incident opening/recovery thresholds with atomic notification events.
- Verified Telegram private-chat linking, delivery history, backoff, and rate-limit handling.
- Availability sample statistics, response-time charts, median and p95, and explicit gaps.
- PostgreSQL coordination across workers, database leases, and late-result fencing.
- OpenAPI/Swagger, migrations, Docker Compose, automated tests, and GitHub Actions.

All repository documentation, API messages, and interface copy are in English.

Release verification: **277 backend tests and 18 frontend regressions passed**. A full controlled 30-minute run passed with 100 monitors and 4.32 million historical checks. See the [recorded measurements](docs/performance.md) and [acceptance evidence](docs/acceptance.md).

![API Watch monitor dashboard](docs/images/dashboard.png)

*Local release verification with intentional status mismatches and configuration changes.*

![Response-time chart with missing measurements left empty](docs/images/charts.png)

## Quick start with Docker

Requirements: Python 3.12+ to generate local secrets, Docker Engine/Desktop, and Docker Compose v2.

```bash
git clone https://github.com/destinesia7187/api-watch.git
cd api-watch
python -m api_watch init-env
docker compose up --build -d
```

Open [API Watch](http://127.0.0.1:8000) and [Swagger UI](http://127.0.0.1:8000/docs). Register an account and create a monitor. The migration service runs before the application processes start.

`init-env` uses only the Python standard library and generates random keys/passwords. It refuses to overwrite an existing `.env`. The Compose deployment uses PostgreSQL even though the local-development URL in `.env` defaults to SQLite.

```bash
docker compose logs -f web worker background
docker compose ps
docker compose down
```

`docker compose down` keeps named volumes. Do not use `--volumes` unless you intend to delete stored data.

## Local development without Docker

Python 3.12+ is required. SQLite is provided for a single local worker/background setup; PostgreSQL is required for production and multiple workers.

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m api_watch init-env
python -m api_watch migrate
```

Run these commands in three terminals with the virtual environment active:

```bash
python -m api_watch web
python -m api_watch worker
python -m api_watch background
```

The web process never starts a hidden scheduler. All three processes are necessary for regular checks and notifications. The background process runs scheduling, lease expiry, retention, Telegram polling/delivery, and password-reset delivery.

For local PostgreSQL, set `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE` in `.env`, migrate, and restart all processes.

## Configure Telegram

1. Create a bot using Telegram's official BotFather workflow.
2. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` in `.env`; never commit the token.
3. Restart the application/background processes.
4. Open **Settings → Telegram**, generate a connection link, and start the bot in your private chat.
5. Send a test notification, then enable notifications for a monitor.

Link codes expire after ten minutes and can be consumed once. Setting a chat ID directly is deliberately unsupported. Checks and incidents remain functional without Telegram configured.

## Password recovery

The public forgot-password response does not reveal whether an email exists. Reset links expire in thirty minutes and can be used once. Changing/resetting a password revokes existing sessions and personal API tokens.

Development without SMTP writes messages to ignored `var/mail/*.txt` files. With Compose, inspect the development mailbox from the background container:

```bash
docker compose exec background python -c "from pathlib import Path; print('\n'.join(p.read_text() for p in Path('var/mail').glob('*.txt')))"
```

These files contain reset links: use them only on your own development machine. Production requires SMTP and HTTPS. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_STARTTLS`.

## API example

Create a personal API token from **Settings → API tokens**. Copy it immediately; its full value is shown once.

```bash
curl http://127.0.0.1:8000/api/v1/monitors \
  -H 'Authorization: Bearer YOUR_API_WATCH_TOKEN' \
  -H 'Content-Type: application/json' \
  --data '{
    "name": "Catalog health",
    "url": "https://api.example.com/health",
    "method": "GET",
    "interval_seconds": 60,
    "timeout_ms": 10000,
    "expected_status": 200,
    "json_assertions": [
      {"pointer": "/status", "operator": "equals", "expected": "ok"}
    ],
    "failure_threshold": 3,
    "recovery_threshold": 2
  }'
```

The example domain is illustrative. Monitor a public endpoint you control. More examples are in [docs/api-examples.md](docs/api-examples.md); the live contract is at `/openapi.json` and a generated snapshot is in [docs/openapi.json](docs/openapi.json).

## How checks and incidents work

- The default interval is 60 seconds; a request has a 10-second total timeout.
- All configured conditions must pass. Redirects are not followed. HTTPS certificate checks cannot be disabled.
- Three consecutive failures open one incident; two consecutive successes confirm recovery by default.
- Internal errors, blocked targets, missed slots, and interruptions do not count as target downtime. They break a confirmation streak.
- Manual checks are diagnostics. They appear in history but do not change scheduled statistics or incidents.
- Pause does not mean recovery. Changing request configuration requires pausing, creates a new version, and aborts an existing incident without claiming recovery.
- Missed slots are recorded rather than replayed as a burst after a restart.
- The database applies each accepted result once. An external HTTP request or Telegram message cannot have a general exactly-once guarantee after an ambiguous network failure.

The success rate is `successful scheduled checks / evaluated scheduled checks`, not a contractual SLA. Missing observations remain missing. Latency statistics use successful scheduled checks only; timeouts are not fake latency samples.

## Tests and checks

```bash
python -m pytest -q
python -m ruff check api_watch tests scripts
python -m compileall -q api_watch migrations
node --check api_watch/static/app.js
node scripts/test_frontend.cjs
```

The application does not need Node.js to run. Node is used for a dependency-free frontend regression harness.

The outbound tests use isolated HTTP/TLS fixtures, controlled DNS, and a test-only host allowlist. They do not rely on public websites or real Telegram credentials. PostgreSQL-specific concurrency tests require an empty disposable database:

```bash
# Linux/macOS:
export TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:5432/apiwatch_test'
# PowerShell:
# $env:TEST_DATABASE_URL = 'postgresql+psycopg://postgres:postgres@127.0.0.1:5432/apiwatch_test'
python -m pytest -q
```

CI provides PostgreSQL automatically. Use a dedicated test database: tests create schema objects and remove their fixture records. Never point `TEST_DATABASE_URL` at production.

## Repository map

```text
api_watch/             API, authentication, scheduler, checker, Telegram, statistics
api_watch/static/      Same-origin browser client (no CDN or frontend build)
migrations/            Versioned Alembic migrations
tests/                 Security, HTTP/TLS, API, scheduling, incident and delivery tests
scripts/               Regression, maintenance, demonstration and load tools
docs/                  Architecture, security, operations, API and measured results
.github/workflows/     Automated verification with PostgreSQL
```

Read [architecture](docs/architecture.md), [security](docs/security.md), [operations](docs/operations.md), [performance](docs/performance.md), and the [technical specification](TECHNICAL_SPECIFICATION.md).

The [design system](docs/design-system.md) describes the dashboard's visual tokens, horizontal navigation and responsive layout. [Font licenses](docs/font-licenses.md) record the sources and licenses for its self-hosted Inter and JetBrains Mono typography.

The [acceptance map](docs/acceptance.md) connects release requirements to concrete tests. The [backup/restore drill](docs/backup-verification.md) and [real worker crash drill](docs/worker-recovery-verification.md) include recorded recovery reports. Run `python -m scripts.demo` for a fast, isolated HTTP failure/recovery demonstration.

## Scope and deployment

Version 1.0 monitors public HTTP/HTTPS endpoints on standard ports from one deployment location. It does not execute browser JavaScript, perform load testing of monitored targets, refresh OAuth tokens, or support private-network probes.

For internet-facing deployment set `APP_ENV=production`, use PostgreSQL, configure HTTPS/SMTP, preserve encryption keys, and apply the documented egress policy. The default Compose port is loopback-only; the repository does not automatically publish a public service. Operational checks must include worker/scheduler heartbeats in addition to the HTTP health endpoint.

No real bot token, SMTP password, production domain, or GitHub remote is bundled. Those belong to the deployment owner. Publishing licensing terms is also the owner's choice; no license grant is assumed by this repository.
