# API examples

Base URL: `http://127.0.0.1:8000/api/v1`. Swagger UI: `/docs`.

Requests below use a personal token created in Settings. Browser-session mutations additionally require `X-CSRF-Token`; obtain the token from login or `GET /auth/csrf`. Do not send a browser cookie and a Bearer token together.

## Create a JSON API monitor

```http
POST /api/v1/monitors
Authorization: Bearer YOUR_API_WATCH_TOKEN
Content-Type: application/json
```

```json
{
  "name": "Catalog API",
  "url": "https://api.example.com/health",
  "method": "GET",
  "headers": [{"name": "Accept", "value": "application/json"}],
  "secret_headers": [{"name": "X-API-Key", "value": "EXTERNAL_SECRET"}],
  "query_params": [{"name": "details", "value": "true"}],
  "auth": {"type": "none"},
  "body": {"enabled": false},
  "interval_seconds": 60,
  "timeout_ms": 10000,
  "expected_status": 200,
  "max_response_time_ms": 2000,
  "json_assertions": [
    {"pointer": "/status", "operator": "equals", "expected": "ok"},
    {"pointer": "/database", "operator": "exists"}
  ],
  "failure_threshold": 3,
  "recovery_threshold": 2,
  "notifications_enabled": true,
  "enabled": true
}
```

The secret is encrypted at rest and omitted from later responses. The secret and built-in Bearer authorization require HTTPS. Query parameters belong in `query_params`, not in `url`.

For a JSON POST request set `method=POST`, `body={"enabled":true,"json":{"probe":true}}`, and `side_effects_acknowledged=true`. Such a request executes repeatedly and may modify the target. There are no automatic retries within a check.

## Pause, edit and resume

Read the current `record_version`, then pause:

```http
POST /api/v1/monitors/MONITOR_ID/pause
Authorization: Bearer YOUR_API_WATCH_TOKEN
Content-Type: application/json
```

```json
{"expected_version": 1}
```

Use the returned new version for PATCH:

```http
PATCH /api/v1/monitors/MONITOR_ID
Authorization: Bearer YOUR_API_WATCH_TOKEN
Content-Type: application/json
```

```json
{
  "expected_version": 2,
  "timeout_ms": 5000,
  "secret_headers": [
    {"name": "X-API-Key", "value": "REPLACEMENT_SECRET"},
    {"name": "X-Old-Secret", "remove": true}
  ]
}
```

Use `POST /monitors/MONITOR_ID/resume` with the latest `expected_version`.

Ordinary arrays replace the saved array. Secret headers are operations by case-insensitive name: omitted names are preserved, `value` replaces, `remove=true` deletes. For `auth`, an explicit `type` is required: `{"type":"none"}` removes a Bearer token; `{"type":"bearer"}` preserves an existing one. A new Bearer configuration needs `token`.

`record_version` changes on user updates. `config_version` changes only when execution parameters change. An outdated expected version returns `409 VERSION_CONFLICT`.

## Manual diagnostic

```http
POST /api/v1/monitors/MONITOR_ID/run-now
Authorization: Bearer YOUR_API_WATCH_TOKEN
Idempotency-Key: 5bb635eb-b73b-4a17-a398-7b36c689110d
```

The response is `202` with `check_id` and `Location`. Poll `GET /checks/CHECK_ID`. Retrying with the same key returns the original check for 24 hours. The diagnostic does not affect incidents, scheduled metrics, or the next scheduled slot.

## History and statistics

```http
GET /api/v1/monitors/MONITOR_ID/checks?limit=20&trigger=scheduled&outcome=failure
GET /api/v1/monitors/MONITOR_ID/stats?from=2026-10-19T00:00:00Z&to=2026-10-19T12:00:00Z
GET /api/v1/monitors/MONITOR_ID/stats/series?from=2026-10-19T00:00:00Z&to=2026-10-19T12:00:00Z&bucket=5m
GET /api/v1/incidents?monitor_id=MONITOR_ID&status=open
```

Replace example dates with a period within retained history. Periods are `[from,to)`. Valid buckets: `1m`, `5m`, `1h`, `1d`; omitting the bucket selects a suitable size. Empty samples return `null`, not 100% uptime or 0 ms. Overall metrics are calculated from original observations, not averages of bucket percentiles.

Lists return `items`, `next_cursor`, `has_more`. Pass the cursor back with the **same filters and period**. Cursors are signed and bound to their owner/resource/filters; do not interpret or modify them.

## Read-only incidents

Incidents are created and finished by the check engine. Statuses: `open`, `resolved`, `aborted`. Configuration changes and monitor deletion produce an aborted observation, not a recovery. Period filters include incidents overlapping the requested interval.

## Account and integration routes

These require a browser session and CSRF protection for mutations:

- `POST /auth/register`, `POST /auth/login` are public entry points.
- `GET /auth/me`, `GET /auth/csrf`, `POST /auth/logout`.
- `POST /auth/password/forgot` accepts email; `POST /auth/password/reset` accepts token/new_password.
- `POST /auth/password` accepts current_password/new_password.
- `DELETE /auth/account` accepts password and deletes owned application records.
- `GET/POST /api-tokens`, `DELETE /api-tokens/ID`.
- `GET /integrations/telegram`, `POST /integrations/telegram/link`, `DELETE /integrations/telegram`.
- `POST /integrations/telegram/test`, `GET /notifications`, `GET /notifications/ID`.

Public password-recovery routes do not require an existing session. Reset tokens are accepted in the request body, never written into access logs. The browser receives the email link in its fragment and removes it before subsequent navigation.

## Error format

```json
{
  "error": {
    "code": "MONITOR_MUST_BE_IDLE_AND_PAUSED",
    "message": "Pause the monitor before changing request settings.",
    "details": [],
    "request_id": "6826f6c7-9ae5-4a18-b8bf-832fc7ba9086"
  }
}
```

Foreign and missing resources both return `404`. Missing/expired authentication returns `401`; insufficient token scope and CSRF errors return `403`; invalid values return `422`; conflicts return `409`; rate limits return `429` with `Retry-After`. Target HTTP errors are represented inside a successful check-resource response as `outcome=failure`.
