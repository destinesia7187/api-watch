from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from api_watch.models import ScheduleGap
from api_watch.statistics import gap_slots, metrics


def test_nearest_rank_and_exact_empty_behavior():
    rows = [SimpleNamespace(outcome="success", response_time_ms=n) for n in range(1, 21)]
    result = metrics(rows)
    assert result["p95_response_time_ms"] == 19
    assert result["p50_response_time_ms"] == 10.5
    assert metrics([])["success_rate_percent"] is None
    assert metrics([SimpleNamespace(outcome="failure", response_time_ms=500)])["avg_response_time_ms"] is None


def test_gap_clipping_uses_slots_not_full_intersecting_range():
    start = datetime(2026, 10, 19, tzinfo=UTC)
    gap = ScheduleGap(first_slot_at=start, last_slot_at=start + timedelta(minutes=9), slot_count=10, interval_seconds=60)
    assert gap_slots(gap, start + timedelta(seconds=61), start + timedelta(minutes=4)) == 2
    assert gap_slots(gap, start + timedelta(minutes=10), start + timedelta(minutes=11)) == 0
