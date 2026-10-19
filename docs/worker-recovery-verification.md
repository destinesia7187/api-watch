# Recovery after a real worker crash

`scripts/verify_worker_recovery.py` exercises AT-16 with a real independently running `python -m api_watch worker` process. The target is a controlled loopback HTTP server; no public site, external API, Telegram bot, or SMTP server is contacted.

The script uses a unique `recovery_<random>` PostgreSQL schema, applies the committed migrations there, and creates its own fixture user and monitor. Existing application tables, other schemas, other worker processes, and the PostgreSQL service are left running. Fixture keys and database settings are supplied to the owned child processes through their environment and are never written to the report.

PowerShell:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://postgres@127.0.0.1:55432/apiwatch_test'
python -m scripts.verify_worker_recovery
```

Linux or macOS:

```sh
TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:5432/apiwatch_test' \
  python -m scripts.verify_worker_recovery
```

The drill takes slightly over one minute:

1. A scheduled POST monitor uses a 60-second interval, a 1-second target timeout, and explicit acknowledgment of side effects. The fixture records receipt before delaying its reply.
2. Immediately after that receipt, the script hard-kills only the worker process it launched. It checks that the database still contains the ambiguous `running` operation.
3. It waits for the actual PostgreSQL clock to pass the real 16-second worker lease. No clock override or timestamp mutation accelerates recovery.
4. The normal lease-expiration function marks the operation `finished / interrupted / worker_lost`. A completion submitted with its old lease token is rejected.
5. A replacement CLI worker runs against the same isolated schema. It cannot requeue or resend the interrupted POST.
6. When the actual next anchored 60-second slot arrives, the normal scheduler queues a new check. The replacement worker sends one new POST and saves a successful result. The original check remains interrupted, and no false incident or notification is created.

The expected result is exactly two POST receipts and two check records across two distinct scheduled slots. The first operation is never replayed, even though the target received it and its outcome was uncertain after the crash.

The script prints progress and writes the secret-free result to `var/worker-recovery-report.json`. Cleanup stops only its owned worker subprocesses, closes the fixture, removes its random schema, and removes the temporary worker log. A failed assertion produces a nonzero exit code. Run without Python's `-O` optimization, which would disable assertions.

This controlled test verifies process loss, durable state, real lease expiration, fencing, and subsequent scheduling. It does not claim that killing a client can undo a side effect already performed by a remote API. See [operations.md](operations.md) for operational crash recovery and supervision.

## Recorded release verification

The drill passed on 19 October 2026 with Windows 11, Python 3.12.14, PostgreSQL 16.15, and committed migration `0001`. All eight assertions passed. The actual worker lease was 16 seconds; after the hard kill, the drill waited 15.953 seconds for that lease to expire. The replacement worker completed the next real anchored slot successfully. Exactly two POSTs and two check records existed, with the original check still `interrupted / worker_lost` and no incident or notification. The complete drill took 63.782 seconds.

Both owned worker processes were stopped and the disposable schema was removed. The retained [machine-readable report](evidence/worker-recovery.json) records the measured timestamps and cleanup evidence. The PostgreSQL server and unrelated application/benchmark schemas remained running.
