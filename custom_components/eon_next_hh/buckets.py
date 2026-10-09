"""Peak/off-peak classification and hourly bucketing. Free of Home Assistant imports."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
SLOT = timedelta(minutes=30)
HOUR = timedelta(hours=1)
SERIES = ("total", "peak", "offpeak")

# Sums are kept as integer micro-kWh so running totals are exact, and therefore identical however the
# readings were split across imports.
MICRO = 1_000_000

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class Window:
    """A local-time range [start, end) in minutes after midnight; crosses midnight when end <= start."""

    start: int
    end: int

    def contains(self, minute: int) -> bool:
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    def __str__(self) -> str:
        return f"{self.start // 60:02d}:{self.start % 60:02d}-{self.end // 60:02d}:{self.end % 60:02d}"


def parse_window(text: str) -> Window:
    """Parse "HH:MM-HH:MM". Times must fall on half-hours; an end of 24:00 means midnight."""
    match = _WINDOW_RE.match(text)
    if not match:
        raise ValueError(f"'{text}' is not in the form HH:MM-HH:MM")
    sh, sm, eh, em = (int(g) for g in match.groups())
    start, end = sh * 60 + sm, eh * 60 + em
    if sh > 23 or sm > 59 or em > 59 or end > 24 * 60:
        raise ValueError(f"'{text}' contains an invalid time")
    if start % 30 or end % 30:
        raise ValueError(f"'{text}' must start and end on the hour or half-hour")
    end %= 24 * 60
    if start == end:
        raise ValueError(f"'{text}' is empty")
    return Window(start, end)


def parse_windows(texts: Iterable[str]) -> list[Window]:
    return [parse_window(t) for t in texts if t.strip()]


def is_offpeak(slot_start: datetime, windows: Sequence[Window], tz: ZoneInfo = LONDON) -> bool:
    """Classify a half-hour by the local wall-clock time at which it starts."""
    local = slot_start.astimezone(tz)
    minute = local.hour * 60 + local.minute
    return any(w.contains(minute) for w in windows)


def to_micro(kwh: float) -> int:
    return round(kwh * MICRO)


def from_micro(value: int) -> float:
    return value / MICRO


@dataclass(frozen=True)
class Sums:
    """Micro-kWh per series."""

    total: int = 0
    peak: int = 0
    offpeak: int = 0

    def __add__(self, other: Sums) -> Sums:
        return Sums(self.total + other.total, self.peak + other.peak, self.offpeak + other.offpeak)

    def __sub__(self, other: Sums) -> Sums:
        return Sums(self.total - other.total, self.peak - other.peak, self.offpeak - other.offpeak)

    def get(self, series: str) -> int:
        return getattr(self, series)


@dataclass(frozen=True)
class HourRow:
    start: datetime  # UTC hour
    state: Sums  # consumption within the hour
    sum: Sums  # running total up to the end of the hour


def floor_hour(moment: datetime) -> datetime:
    moment = moment.astimezone(UTC)
    return moment.replace(minute=0, second=0, microsecond=0)


def floor_slot(moment: datetime) -> datetime:
    moment = moment.astimezone(UTC)
    return moment.replace(minute=moment.minute - moment.minute % 30, second=0, microsecond=0)


def hourly_buckets(
    readings: Iterable[tuple[datetime, float]], windows: Sequence[Window], tz: ZoneInfo = LONDON
) -> dict[datetime, Sums]:
    """Classify each half-hour, then add it into its UTC hour. UK offsets are whole hours, so UTC hours are local hours."""
    by_slot = {start.astimezone(UTC): kwh for start, kwh in readings}
    buckets: dict[datetime, Sums] = {}
    for start in sorted(by_slot):
        kwh = to_micro(by_slot[start])
        part = Sums(kwh, 0, kwh) if is_offpeak(start, windows, tz) else Sums(kwh, kwh, 0)
        hour = floor_hour(start)
        buckets[hour] = buckets.get(hour, Sums()) + part
    return buckets


def running_totals(buckets: dict[datetime, Sums], base: Sums) -> list[HourRow]:
    rows: list[HourRow] = []
    running = base
    for hour in sorted(buckets):
        running = running + buckets[hour]
        rows.append(HourRow(hour, buckets[hour], running))
    return rows


def missing_slots(
    present: Iterable[datetime], start: datetime, end: datetime, tz: ZoneInfo = LONDON
) -> list[datetime]:
    """Half-hours starting in [start, end) with no reading.

    E.ON reports the repeated local hour on the day the clocks go back as one doubled reading on the first
    pass, so the second pass is not counted as missing when the first is present.
    """
    have = {p.astimezone(UTC) for p in present}
    missing = []
    slot = floor_slot(start)
    if slot < start:
        slot += SLOT
    while slot < end:
        if slot not in have and not (slot.astimezone(tz).fold and slot - HOUR in have):
            missing.append(slot)
        slot += SLOT
    return missing


def compress_ranges(slots: Sequence[datetime]) -> list[tuple[datetime, datetime]]:
    """Merge consecutive half-hours into [start, end) ranges."""
    ranges: list[tuple[datetime, datetime]] = []
    for slot in sorted(slots):
        if ranges and ranges[-1][1] == slot:
            ranges[-1] = (ranges[-1][0], slot + SLOT)
        else:
            ranges.append((slot, slot + SLOT))
    return ranges
