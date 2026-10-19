# Operations and recovery

Run commands from the repository root. The README contains the clean-clone setup and platform prerequisites. The Docker Compose deployment uses PostgreSQL and separate `web`, `worker`, and `background` services. The background service runs independent scheduler, retention, Telegram dispatch/polling, and password-reset mail loops.

## Configuration and migration

1. Create a virtual environment and install the locked dependencies as described in the README.
2. Run `python -m api_watch init-env` once to generate `.env` with random development secrets. It refuses to overwrite an existing file.
3. For Compose, keep the generated `POSTGRES_PASSWORD`, `SESSION_SECRET`, `ENCRYPTION_KEY` and matching key ID. Compose supplies the internal PostgreSQL URL.
4. Run the explicit migration step before starting application processes. Local Python: `python -m api_watch migrate`. Compose: `docker compose run --rm migrate` (or the README's dependency-aware first-start command).
5. Start web, background and worker processes. Do not run a second embedded scheduler inside the web application.

`APP_ENV=production` requires an HTTPS `PUBLIC_BASE_URL`, PostgreSQL, generated session/encryption secrets and SMTP. Terminate TLS at a configured reverse proxy and preserve the intended public origin. The default Compose web port binds to `127.0.0.1:8000`; the database is not published.

Set `FORWARDED_ALLOW_IPS` to the exact trusted proxy addresses when deploying behind a reverse proxy. Uvicorn uses this trust list for forwarded client IP and scheme headers; the default trusts loopback only. A containerized proxy may reach the web service from a different address. Preserve real client addresses so IP-based authentication limits apply to each client, and keep the web listener restricted to the intended proxy path.

Do not replace a working `.env` when upgrading. Review configuration changes and back up the database and matching keys before applying a new migration. Application processes never apply migrations automatically.

## Process supervision and diagnostics

| Command or endpoint | Purpose |
| --- | --- |
| `python -m api_watch web` | Start API and dashboard on the configured local bind |
| `python -m api_watch worker` | Process durable checks |
| `python -m api_watch background` | Run scheduler, cleanup, Telegram and mail services |
| `python -m api_watch status` | Print persisted service heartbeat timestamps |
| `python -m api_watch cleanup` | Execute a bounded retention batch explicitly |
| `/health/live` | Check that the web process can respond |
| `/health/ready` | Verify database access and expected migration revision |
| `/api/v1/service-status` | Authenticated persisted process health timestamps |

Readiness and worker health are separate. A reachable web server does not prove checks are running. Inspect `worker`, `scheduler`, `notifier` and `telegram_poller` heartbeats; a timestamp older than 60 seconds is shown as unhealthy. An unconfigured Telegram channel is reported as such without disabling checks.

Use `docker compose logs --tail=100 web worker background` for operational errors. Logs intentionally omit configured URLs/queries, response bodies, headers, bot URLs and raw client exceptions. An API error includes a request ID for correlation.

Useful diagnostic SQL (run against the application database):

```sql
SELECT status, outcome, count(*) FROM check_runs GROUP BY status, outcome;
SELECT min(created_at) AS oldest_queued, count(*) AS queued
FROM check_runs WHERE status = 'queued';
SELECT count(*) AS expired_running FROM check_runs
WHERE status = 'running' AND lease_expires_at <= clock_timestamp();
SELECT status, count(*) FROM notification_outbox GROUP BY status;
SELECT id, last_seen_at, details FROM service_heartbeats ORDER BY id;
```

If the database is unavailable, workers do not start new requests. If a worker disappears, its lease expires and the background service marks the run `interrupted / worker_lost`. Never reset such a run to `queued`: its external request might already have changed state. Restart processes and allow the next normal scheduled slot to run.

If the scheduler was down, old slots are stored as aggregate gaps and only the latest current slot is queued. A missing observation is not a target failure or evidence of uptime. A monitor showing `unknown` can still have an open incident.

## Telegram setup and failures

Create a bot with Telegram's BotFather, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`, and restart background/web processes. Use the dashboard's connection link from a private chat; links expire after 10 minutes. Keep exactly one logical polling stream for this bot; do not configure a competing webhook or unrelated `getUpdates` consumer.

The dashboard shows delivery status. Transient network/5xx failures retry up to six attempts with 30, 60, 120, 240 and 480 second backoffs plus ±10% jitter. A `429` also honors Telegram's `retry_after`. Permanent rejection such as `403` stops retrying. Check that the bot is not blocked before reconnecting and sending an explicit test notification.

Disconnect/relink cancels old pending delivery generations. Enabling Telegram does not replay previously suppressed incidents. A retry after an uncertain response can duplicate a message; compare incident IDs. Never edit a queued row's encrypted recipient manually.

## Password recovery and mail

Set `SMTP_HOST`, port, optional username/password, `SMTP_FROM` and `SMTP_STARTTLS`. Production startup requires SMTP. Development without SMTP writes reset messages under ignored `var/mail`; the Compose `local-mail` volume persists these development files. Treat those files as secrets and remove them when no longer needed. Reset delivery payloads are encrypted in the database and erased after delivery or terminal failure.

Password reset links expire after 30 minutes and can be used once. A password reset/change revokes all active sessions and API tokens; users sign in again and issue new API tokens deliberately.

## Retention

The scheduler executes small cleanup batches every minute. Each table cleanup is bounded to 1,000 rows per iteration; an unusually large backlog drains through subsequent iterations. Operators can call `cleanup` repeatedly after observing a backlog.

| Data | Default retention |
| --- | --- |
| Finished check detail and schedule gaps | 30 days |
| Resolved/aborted incidents | 180 days after ending |
| Open incidents | Kept while open |
| Terminal notification delivery history | 30 days after final delivery state |
| Expired idempotency and linking/reset codes | Removed by the next cleanup batches |
| Old configuration versions | Removed when no retained check/incident needs them |
| Deleted monitors | All remaining rows removed after 180 days |

Deleted monitor request payloads are erased immediately after cancellation, ahead of history expiry. Safe version timing/threshold metadata can remain for referential history. Account deletion removes owned data immediately through foreign-key cascades.

## Back up PostgreSQL and key material

The database alone cannot recover encrypted configurations. Keep a protected backup of `ENCRYPTION_KEY`, `ENCRYPTION_KEY_ID`, and relevant deployment settings with each database backup. Back up the session secret if retaining existing sessions is intentional; rotating it invalidates session-derived CSRF/cursor signatures. Never commit backup files or `.env` to Git.

For portable Windows/Linux commands, use `docker compose cp` instead of redirecting a binary dump through a PowerShell text pipeline:

```text
docker compose exec -T db pg_dump -U apiwatch -d apiwatch -Fc -f /tmp/api-watch.dump
docker compose cp db:/tmp/api-watch.dump ./api-watch.dump
```

Copy the dump and matching secrets to encrypted off-host storage with restricted access. Record the application commit, migration revision, PostgreSQL version and backup time. Back up before upgrades and key rotation.

## Restore drill

Use a separate empty database or disposable Compose project. Stop all web/background/worker writers while restoring. Point the restored application at the matching encryption key and key ID before starting it.

```text
docker compose cp ./api-watch.dump db:/tmp/api-watch.dump
docker compose exec -T db pg_restore -U apiwatch -d apiwatch --clean --if-exists --no-owner /tmp/api-watch.dump
python -m api_watch migrate
```

The final migration command is for a local Python process connected to the restored database; use `docker compose run --rm migrate` in a Compose deployment. The `--clean` restore removes objects in the named database: verify the destination is the intended disposable/restore database before using it.

After restore, verify:

1. Migration/readiness succeeds and a test user can sign in.
2. Monitor configurations can be decrypted and secrets remain masked through the API.
3. Retained history, incident timestamps and notification statuses are present.
4. Stale running leases become interrupted; no old HTTP operation is replayed.
5. Scheduler/worker heartbeats resume and a new safe test check completes.
6. Telegram is deliberately enabled for the restored environment, avoiding duplicate production alerts from a parallel clone.

Record the restore result and elapsed time. A backup is operationally proven only after this drill has actually succeeded. See [performance.md](performance.md) for which release validations have been measured.

## Encryption key rotation

There is one active AES-GCM key and key ID; an online multi-key keyring is not implemented. Changing only environment values breaks decryption. Rotation is an offline maintenance operation:

1. Stop web, workers and background services, and verify no old process remains.
2. Back up the database and old key/ID together. Preserve this pair until restore verification succeeds.
3. Generate a new random 32-byte URL-safe base64 key and a new key ID.
4. Set `NEW_ENCRYPTION_KEY` and `NEW_ENCRYPTION_KEY_ID` through protected environment/secret storage, then run `python scripts/rotate_key.py` with the current old key settings still active. The utility decrypts every nonempty encrypted payload and re-encrypts it in one transaction: monitor configurations, Telegram connections, notification recipient snapshots and password-reset email payloads. It also updates configuration `key_id` values. It does not print keys or overwrite `.env`.
5. Update `.env`/secret storage only after the transaction succeeds, restart all processes with the new key/ID and verify a configuration, Telegram recipient and pending reset payload can be read.
6. Perform a new backup and restore drill with the new key pair. Keep historical backups paired with their historical keys.

If verification fails, stop writers and restore the pre-rotation database and old key together. Do not attempt to recover by mixing old and new key identifiers.
