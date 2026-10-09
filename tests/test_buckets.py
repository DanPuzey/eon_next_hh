"""Peak/off-peak classification and hourly bucketing, including the BST changes."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.eon_next_hh.buckets import (
    LONDON,
    SLOT,
    Sums,
    Window,
    compress_ranges,
    hourly_buckets,
    is_offpeak,
    missing_slots,
    parse_window,
    parse_windows,
    running_totals,
    to_micro,
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def local_day_slots(year: int, month: int, day: int) -> list[datetime]:
    start = datetime(year, month, day, tzinfo=LONDON).astimezone(UTC)
    end = (datetime(year, month, day, tzinfo=LONDON) + timedelta(days=1)).replace(tzinfo=LONDON).astimezone(UTC)
    slots = []
    while start < end:
        slots.append(start)
        start += SLOT
    return slots


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00:00-07:00", Window(0, 420)),
        ("23:30-05:30", Window(1410, 330)),
        (" 7:00 - 24:00 ", Window(420, 0)),
        ("12:30-13:00", Window(750, 780)),
    ],
)
def test_parse_window(text: str, expected: Window) -> None:
    assert parse_window(text) == expected


@pytest.mark.parametrize("text", ["", "7-8", "00:00-00:00", "25:00-01:00", "00:15-07:00", "00:00-24:30", "10:00-10:60"])
def test_parse_window_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_window(text)


def test_window_round_trips_to_text() -> None:
    assert [str(w) for w in parse_windows(["23:30-05:30", "", "13:00-16:00"])] == ["23:30-05:30", "13:00-16:00"]


def test_window_crossing_midnight() -> None:
    window = parse_window("23:30-05:30")
    inside = [23 * 60 + 30, 23 * 60 + 59, 0, 3 * 60, 5 * 60]
    outside = [5 * 60 + 30, 12 * 60, 23 * 60]
    assert all(window.contains(m) for m in inside)
    assert not any(window.contains(m) for m in outside)


def test_classifies_by_local_time_in_summer_and_winter() -> None:
    windows = parse_windows(["00:00-07:00"])
    # 23:30 UTC is 00:30 BST (off-peak) in summer but 23:30 GMT (peak) in winter.
    assert is_offpeak(utc(2026, 7, 1, 23, 30), windows)
    assert not is_offpeak(utc(2026, 1, 1, 23, 30), windows)
    # 06:30 UTC is 07:30 BST (peak) in summer but 06:30 GMT (off-peak) in winter.
    assert not is_offpeak(utc(2026, 7, 1, 6, 30), windows)
    assert is_offpeak(utc(2026, 1, 1, 6, 30), windows)


def test_spring_forward_day_has_23_hours() -> None:
    slots = local_day_slots(2026, 3, 29)
    assert len(slots) == 46
    windows = parse_windows(["00:00-07:00"])
    buckets = hourly_buckets([(s, 1.0) for s in slots], windows)
    assert len(buckets) == 23
    # Local 00:00-01:00 GMT, then 02:00-07:00 BST: six off-peak hours, because 01:00-02:00 never happens.
    offpeak_hours = [h for h, v in buckets.items() if v.offpeak]
    assert len(offpeak_hours) == 6
    assert sum(v.offpeak for v in buckets.values()) == to_micro(12.0)
    assert sum(v.peak for v in buckets.values()) == to_micro(34.0)
    assert min(offpeak_hours) == utc(2026, 3, 29, 0) and max(offpeak_hours) == utc(2026, 3, 29, 5)


def test_fall_back_day_has_25_hours() -> None:
    slots = local_day_slots(2026, 10, 25)
    assert len(slots) == 50
    windows = parse_windows(["00:00-07:00"])
    buckets = hourly_buckets([(s, 1.0) for s in slots], windows)
    assert len(buckets) == 25
    # 01:00-02:00 local happens twice, both off-peak: eight off-peak hours.
    assert sum(1 for v in buckets.values() if v.offpeak) == 8
    assert sum(v.offpeak for v in buckets.values()) == to_micro(16.0)
    assert sum(v.peak for v in buckets.values()) == to_micro(34.0)
    assert buckets[utc(2026, 10, 25, 0)].offpeak == to_micro(2.0)  # 01:00 BST
    assert buckets[utc(2026, 10, 25, 1)].offpeak == to_micro(2.0)  # 01:00 GMT
    assert buckets[utc(2026, 10, 25, 7)].peak == to_micro(2.0)  # 07:00 GMT


def test_window_boundary_at_the_skipped_hour() -> None:
    # A window starting at 01:00 local begins at 02:00 BST on the spring-forward day.
    windows = parse_windows(["01:00-03:00"])
    offpeak = [s for s in local_day_slots(2026, 3, 29) if is_offpeak(s, windows)]
    assert [s.astimezone(LONDON).strftime("%H:%M") for s in offpeak] == ["02:00", "02:30"]
    # ...and covers both passes of 01:00-02:00 on the fall-back day.
    offpeak = [s for s in local_day_slots(2026, 10, 25) if is_offpeak(s, windows)]
    assert len(offpeak) == 6


def test_hourly_buckets_split_peak_and_offpeak_within_an_hour() -> None:
    windows = parse_windows(["16:30-19:00"])
    buckets = hourly_buckets([(utc(2026, 1, 5, 16), 0.4), (utc(2026, 1, 5, 16, 30), 0.25)], windows)
    assert buckets == {utc(2026, 1, 5, 16): Sums(total=650_000, peak=400_000, offpeak=250_000)}


def test_running_totals_continue_from_base() -> None:
    buckets = {utc(2026, 1, 1, 1): Sums(2, 1, 1), utc(2026, 1, 1, 0): Sums(3, 3, 0), utc(2026, 1, 1, 3): Sums(5, 0, 5)}
    rows = running_totals(buckets, base=Sums(100, 60, 40))
    assert [r.start.hour for r in rows] == [0, 1, 3]
    assert [r.sum for r in rows] == [Sums(103, 63, 40), Sums(105, 64, 41), Sums(110, 64, 46)]


def test_totals_are_exact_whatever_the_split() -> None:
    rng = random.Random(1)
    slots = [utc(2026, 2, 1) + SLOT * i for i in range(48 * 10)]
    readings = [(s, round(rng.uniform(0, 3), 3)) for s in slots]
    windows = parse_windows(["00:00-07:00"])

    whole = running_totals(hourly_buckets(readings, windows), Sums())

    shuffled = readings[:]
    rng.shuffle(shuffled)
    assert running_totals(hourly_buckets(shuffled, windows), Sums()) == whole

    # Import in several pieces, each continuing from the previous piece's final sums.
    pieces, base = [], Sums()
    for cut in range(0, len(readings), 37 * 2):
        rows = running_totals(hourly_buckets(readings[cut : cut + 74], windows), base)
        pieces += rows
        base = rows[-1].sum
    assert pieces == whole
    assert whole[-1].sum.total == whole[-1].sum.peak + whole[-1].sum.offpeak


def test_missing_slots_and_ranges() -> None:
    start = utc(2026, 1, 1)
    present = [start + SLOT * i for i in range(10) if i not in (2, 3, 7)]
    missing = missing_slots(present, start, start + SLOT * 10)
    assert missing == [start + SLOT * 2, start + SLOT * 3, start + SLOT * 7]
    assert compress_ranges(missing) == [(start + SLOT * 2, start + SLOT * 4), (start + SLOT * 7, start + SLOT * 8)]


def test_merged_repeat_hour_is_not_missing() -> None:
    # E.ON returns 01:00 BST doubled and omits 01:00 GMT on the day the clocks go back.
    slots = local_day_slots(2025, 10, 26)
    present = [s for s in slots if s != utc(2025, 10, 26, 1)]
    assert missing_slots(present, slots[0], slots[-1] + SLOT) == []
    # Losing the first pass as well is a real gap.
    present.remove(utc(2025, 10, 26, 0))
    assert missing_slots(present, slots[0], slots[-1] + SLOT) == [utc(2025, 10, 26, 0), utc(2025, 10, 26, 1)]
