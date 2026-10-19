# PostgreSQL backup and restore verification

`scripts/verify_backup_restore.py` is the repeatable AT-34 recovery drill. It uses actual `pg_dump -Fc` and `pg_restore --exit-on-error`, the application's committed Alembic migrations, encrypted configurations, and the real incident transition functions.

The supplied `TEST_DATABASE_URL` provides local server connection parameters. The script never reads or changes the database named by that URL. It creates two unique databases named `apiwatch_backup_source_<random>` and `apiwatch_backup_restore_<random>`, then removes only those databases. It accepts a loopback PostgreSQL host and an explicit database user. The user must have permission to create disposable databases.

Run from the repository root with the virtual environment active. PostgreSQL client tools must match the test server's major version or support that server version.

PowerShell:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://postgres@127.0.0.1:55432/apiwatch_test'
python -m scripts.verify_backup_restore --pg-bin .tools/pgsql/bin
```

Linux or macOS with PostgreSQL tools on `PATH`:

```sh
TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:5432/apiwatch_test' \
  python -m scripts.verify_backup_restore
```

The fixture creates a user and API token, an encrypted authenticated monitor with an exact fractional JSON assertion, nine check records, one resolved and one open incident, and three pending notification events. Incident and outbox records are generated through the actual three-failure/two-success state transitions. A captured expired running lease represents a worker that disappeared after its request may have been sent.

Verification checks:

- Every restored table has the same row count and canonical content fingerprint as the source snapshot, including the Alembic version.
- The matching separately retained key decrypts the exact monitor configuration and Telegram recipient snapshots; a wrong key fails authenticated decryption.
- History, incident check references, event ownership, and the unique incident/event pairs remain consistent.
- The restored application passes readiness, accepts the user's password and existing read API token, masks target secrets, and preserves the fractional JSON value.
- Expiring the captured running lease produces `interrupted / worker_lost` without creating another HTTP operation or duplicate notification.

The script does not start a scheduler, check worker, Telegram dispatcher, or SMTP sender. No target HTTP request or Telegram message is sent. Test fixture encryption keys stay in memory and are destroyed when the process exits. This is a controlled recovery verification, not a backup of an existing deployment or evidence of production recovery time.

The secret-free machine-readable result is written to `var/backup-report.json`. It records PostgreSQL version, migration revision, table counts, dump size and SHA-256, snapshot fingerprints, elapsed timings, individual assertions, and cleanup status. Temporary dump files and the two created databases are removed even after a failed assertion. Nonzero exit status means the drill failed; inspect the report and rerun after fixing the cause. See [operations.md](operations.md) for production backup storage, key preservation, writer shutdown, and recovery procedures.

## Recorded release verification

The drill passed on 19 October 2026 using PostgreSQL 16.15 for Windows x64 and migration `0001`. All 12 recovery assertions passed. The 44,523-byte custom-format dump took 0.25 seconds to create and 0.36 seconds to restore; the complete fixture, application checks, and cleanup took 4.813 seconds. Source and restored fingerprints matched across all 19 tables, including nine checks, two incidents, and three pending notification events. Both disposable databases and the temporary dump were removed.

The retained [machine-readable report](evidence/backup-restore.json) contains the measured evidence. These timings apply to this small local fixture and do not estimate recovery time for a production database or a full 30-day history.
