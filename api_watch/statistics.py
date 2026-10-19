"""Explicit observation-based metrics; missing observations never become uptime."""

import math
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api_watch.api_utils import Principal, get_db, principal, rate_limit
from api_watch.db import utcnow
from api_watch.contracts import StatsRead, SeriesRead
from api_watch.errors import APIError
from api_watch.models import CheckRun, ScheduleGap
from api_watch.monitors import owned_monitor

router = APIRouter(prefix="/api/v1", tags=["Statistics"])
BUCKETS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}


def validate_period(start, end, settings):
    now = utcnow()
    end = end or now
    start = start or end - timedelta(hours=24)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise APIError(422, "INVALID_PERIOD", "Use a nonempty period with timezone-aware from/to values.")
    if end > now + timedelta(seconds=5):
        raise APIError(422, "INVALID_PERIOD", "The period cannot end in the future.")
    earliest = now - timedelta(days=settings.history_days)
    if start < earliest - timedelta(seconds=5) or end - start > timedelta(days=30):
        raise APIError(422, "PERIOD_OUTSIDE_RETENTION", "The requested period is outside retained history.",
                       [{"available_from": earliest.isoformat(), "available_to": now.isoformat()}])
    return start.astimezone(UTC), end.astimezone(UTC)


def metrics(rows, skipped=0):
    counts = Counter(row.outcome for row in rows)
    samples = sorted(row.response_time_ms for row in rows
                     if row.outcome == "success" and row.response_time_ms is not None)
    evaluated = counts["success"] + counts["failure"]
    return {
        "success_count": counts["success"], "failure_count": counts["failure"], "evaluated_count": evaluated,
        "success_rate_percent": round(100 * counts["success"] / evaluated, 4) if evaluated else None,
        "blocked_count": counts["blocked"], "interrupted_count": counts["interrupted"],
        "cancelled_count": counts["cancelled"],
        "non_evaluated_count": sum(counts[x] for x in ["blocked", "interrupted", "cancelled"]),
        "skipped_slot_count": counts["skipped"] + skipped,
        "latency_sample_count": len(samples),
        "avg_response_time_ms": statistics.fmean(samples) if samples else None,
        "min_response_time_ms": samples[0] if samples else None,
        "max_response_time_ms": samples[-1] if samples else None,
        "p50_response_time_ms": statistics.median(samples) if samples else None,
        "p95_response_time_ms": samples[math.ceil(0.95 * len(samples)) - 1] if samples else None,
    }


def load_data(db, monitor_id, start, end, version):
    event_time = func.coalesce(CheckRun.started_at, CheckRun.scheduled_at)
    # Statistics need only these scalar values. Avoid materializing tens of thousands
    # of full ORM entities (including error JSON and lease data) for a 30-day view.
    stmt = select(CheckRun.outcome, CheckRun.response_time_ms, CheckRun.config_version,
                  CheckRun.started_at, CheckRun.scheduled_at).where(
        CheckRun.monitor_id == monitor_id, CheckRun.trigger == "scheduled",
        CheckRun.status == "finished", event_time >= start, event_time < end)
    gap_stmt = select(ScheduleGap).where(ScheduleGap.monitor_id == monitor_id,
        ScheduleGap.first_slot_at < end, ScheduleGap.last_slot_at >= start)
    if version is not None:
        stmt = stmt.where(CheckRun.config_version == version)
        gap_stmt = gap_stmt.where(ScheduleGap.config_version == version)
    return list(db.execute(stmt)), list(db.scalars(gap_stmt))


def gap_slots(gap, start, end):
    first = max(0, math.ceil((start - gap.first_slot_at).total_seconds() / gap.interval_seconds))
    last = min(gap.slot_count, math.ceil((end - gap.first_slot_at).total_seconds() / gap.interval_seconds))
    return max(0, last - first)


@router.get("/monitors/{monitor_id}/stats", response_model=StatsRead)
def monitor_stats(monitor_id: str, request: Request,
                  from_time: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                  config_version: int | None = None,
                  auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    owned_monitor(db, monitor_id, auth.user.id)
    rate_limit(db, f"stats:{auth.user.id}", 20, 60)
    start, end = validate_period(from_time, to, request.app.state.settings)
    rows, gaps = load_data(db, monitor_id, start, end, config_version)
    versions = sorted({row.config_version for row in rows} | {gap.config_version for gap in gaps})
    return {"from": start, "to": end, "config_versions": versions,
            **metrics(rows, sum(gap_slots(gap, start, end) for gap in gaps))}


@router.get("/monitors/{monitor_id}/stats/series", response_model=SeriesRead)
def monitor_series(monitor_id: str, request: Request,
                   from_time: datetime | None = Query(None, alias="from"), to: datetime | None = None,
                   bucket: str | None = None, config_version: int | None = None,
                   auth: Principal = Depends(principal), db: Session = Depends(get_db)):
    owned_monitor(db, monitor_id, auth.user.id)
    rate_limit(db, f"stats:{auth.user.id}", 20, 60)
    start, end = validate_period(from_time, to, request.app.state.settings)
    duration = (end - start).total_seconds()
    recommend = next((name for name, size in BUCKETS.items() if math.ceil(duration / size) + 1 <= 1000), "1d")
    if bucket is None:
        bucket = recommend
    if bucket not in BUCKETS:
        raise APIError(422, "INVALID_BUCKET", "bucket must be 1m, 5m, 1h, or 1d.")
    size = BUCKETS[bucket]
    floor = datetime.fromtimestamp((int(start.timestamp()) // size) * size, UTC)
    count = math.ceil((end - floor).total_seconds() / size)
    if count > 1000:
        raise APIError(422, "TOO_MANY_BUCKETS", "The maximum is 1000 chart points.", [{"recommended_bucket": recommend}])
    rows, gaps = load_data(db, monitor_id, start, end, config_version)
    grouped = {}
    for row in rows:
        event_time = row.started_at or row.scheduled_at
        index = int((event_time - floor).total_seconds() // size)
        grouped.setdefault(index, []).append(row)
    items = []
    for index in range(count):
        low = max(start, floor + timedelta(seconds=index * size))
        high = min(end, floor + timedelta(seconds=(index + 1) * size))
        skipped = sum(gap_slots(gap, low, high) for gap in gaps)
        items.append({"bucket_start": low, "bucket_end": high, **metrics(grouped.get(index, []), skipped)})
    return {"from": start, "to": end, "bucket": bucket, "items": items,
            "config_versions": sorted({row.config_version for row in rows} | {gap.config_version for gap in gaps})}
