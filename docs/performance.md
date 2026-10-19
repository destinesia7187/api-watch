# Performance and validation evidence

This document separates release targets from measurements. A working scheduler or a passing unit suite does not establish a 30-day capacity claim.

## Target workload

The initial acceptance workload is 100 active monitors, each on a 60-second interval, with a deployment-wide concurrency limit of 10 and a limit of two connections to each validated destination IP. Distinct fixture destinations or deliberate destination contention must be reported; a one-IP fixture exercises the two-connection cap rather than the full global throughput.

At this rate, detailed history grows by approximately 144,000 results per day, or 4.32 million results over 30 days before retention. Real storage size depends on safe error metadata and indexes. Do not estimate disk requirements solely from the row count.

| Measurement | Acceptance target |
| --- | --- |
| Sustained workload | 100 monitors for at least 30 minutes |
| Duplicate active logical checks | Zero |
| Confirmed incidents/outbox events | Exactly the expected transition count |
| p95 scheduling delay under the stated controlled load | At most 5 seconds |
| Web/API normal read p95 | At most 500 ms in the acceptance environment |
| 24-hour statistics query p95 | At most 1 second |
| 30-day statistics query p95 | At most 2 seconds on representative populated data |

These targets are acceptance goals from the project specification. They are not production SLAs.

## Reproduction method

Use PostgreSQL, the committed dependency lock/migrations, and a controlled HTTP fixture. Record OS, CPU, RAM, Python/PostgreSQL versions, worker count, database settings, fixture response size/delay and application commit. Use `APP_ENV=test` with the exact explicit fixture host allowlist; do not expose this environment publicly.

1. Create 100 monitors with an anchored 60-second interval and known success/failure/recovery phases.
2. Run separate web, scheduler/background and worker processes. Exercise multiple scheduler/worker processes as a coordination test in addition to the single-scheduler nominal load.
3. Observe the full 30-minute period. Sample scheduled-to-start delay, queue age, worker utilization, throughput, resource use, database timings and notification backlog. Report percentiles over individual observations, not averages of percentiles.
4. Kill a worker after a request is sent. Verify interruption, no requeue/replay, rejection of a late fenced result, and normal scheduling afterward.
5. Stop the scheduler for several intervals. Verify exact aggregated missing-slot counts and no catch-up request storm.
6. Populate representative 24-hour and 30-day history, including failures, skips, gaps and configuration versions. Benchmark statistics endpoints after a documented warm-up and report sample count, median, p95 and maximum.
7. Assert no duplicate active monitor runs or open incidents using database constraints and independent SQL counts.

For a high-cardinality history load, inspect query plans and memory use as well as elapsed time. Statistics select only the scalar fields needed for aggregation, rather than materializing complete ORM history objects. Performance evidence should include the maximum requested 30-day period. A different storage/query strategy must preserve the documented statistical definitions.

## Recorded release measurements

The complete controlled workload passed on October 19, 2026: 100 monitors, five API users, 120 seconds of warm-up and 1,800 measured seconds. PostgreSQL contained 4,320,000 seeded historical checks with success, failure, interrupted and skipped outcomes across two configuration versions. The fixture used one loopback destination with 50–770 ms response delays and an intentional three-failure/two-success cycle for every monitor.

Environment: Windows 11 x64, Python 3.12.14, PostgreSQL 16.15, 12 logical CPU cores. Real HTTP passed through Uvicorn. The benchmark ran web, scheduler and worker tasks concurrently in one Python process; it does not measure a distributed deployment. Other release checks ran on the same host during part of the test.

| Measurement | Samples | Median | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Scheduling delay | 3,001 | 751.25 ms | 1,411.72 ms | 3,170.14 ms |
| Monitor list | 4,472 | 141 ms | 281 ms | 1,594 ms |
| Check history | 2,233 | 31 ms | 94 ms | 1,453 ms |
| 24-hour statistics | 745 | 78 ms | 156 ms | 234 ms |
| 30-day statistics | 745 | 828 ms | 1,344 ms | 2,422 ms |
| 24-hour chart series | 747 | 109 ms | 219 ms | 1,547 ms |

All 8,942 measured API responses were HTTP 200. The run observed zero duplicate active checks, no more than ten running checks and no more than two destination leases per IP. Exactly 100 incidents resolved and 200 durable opening/recovery events were recorded. All ten assertions in the load report passed. Limits are p95 targets; the table also exposes slower individual responses.

The [machine-readable load report](evidence/load-test.json) contains the actual sample sizes, outcomes, timings and acceptance decisions. Generated schema data was removed after the run.

## Reproduce the load test

Set TEST_DATABASE_URL to a dedicated local PostgreSQL test database, activate the virtual environment, and run from the repository root:

```sh
python -m scripts.load_test
```

The default run generates 30 days of history, warms up for two minutes and measures for thirty minutes. It uses an exact test-only loopback allowlist and a unique isolated schema, then removes that schema. Allow enough local disk space for millions of rows and PostgreSQL WAL. Never use a production database. Results are written to var/load-report.json; nonzero exit means at least one acceptance condition failed. Shorter/history-free runs are smoke tests and intentionally do not pass the duration/history gates.

## Other release evidence

The [validation report](evidence/validation.json) records 277 passing backend tests with real PostgreSQL, three SQLite-only parametrization skips, 15 passing frontend regressions, static checks, deterministic OpenAPI, wheel build and Compose configuration validation. A local Docker image build/start was unavailable because Docker Engine is absent; CI includes a reproducible image-build job.

A separate [real worker crash drill](worker-recovery-verification.md) killed an actual CLI worker after POST receipt, waited for its real lease to expire and verified no replay before the next anchored slot. The [backup/restore drill](backup-verification.md) verifies a real PostgreSQL custom-format dump/restore, encrypted data and restored application access. These reports give their own fixture sizes and scope.

These measurements establish this controlled local workload. They do not define an SLA or predict other hardware, production traffic, external network behavior, SMTP delivery or real Telegram availability. Repeat the documented workload on the intended deployment before making capacity commitments.
