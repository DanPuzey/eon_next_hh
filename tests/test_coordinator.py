"""Backfill, rolling re-import and running totals, against a real recorder."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.eon_next_hh.api import EonNextAuthError
from custom_components.eon_next_hh.buckets import LONDON
from custom_components.eon_next_hh.const import DOMAIN, SERVICE_BACKFILL

from .common import (
    FakeEonNext,
    connection_error,
    make_entry,
    make_readings,
    reference_statistics,
    refresh,
    sensor_state,
    setup_entry,
    stored_statistics,
)

NOW = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
HOUR_START = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
LAG = timedelta(days=1)  # E.ON's half-hourly data typically arrives about a day late
NIGHT = [(time(0, 0), time(7, 0))]


@pytest.fixture
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(NOW)
    return freezer


async def test_initial_backfill_pages_backwards_and_imports_history(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    readings = make_readings(NOW - timedelta(days=10), NOW - LAG)
    fake = FakeEonNext(readings)
    entry = make_entry(["00:00-07:00"], backfill_days=10)

    await setup_entry(hass, entry, fake)

    backfill_calls = fake.calls[:2]
    assert backfill_calls == [
        (HOUR_START - timedelta(days=7), HOUR_START),
        (HOUR_START - timedelta(days=10), HOUR_START - timedelta(days=7)),
    ]
    assert await stored_statistics(hass) == reference_statistics(readings, NIGHT)
    assert sensor_state(hass, "sync_status").state == "ok"
    assert sensor_state(hass, "last_data").state == (NOW - LAG).isoformat()
    assert sensor_state(hass, "missing_intervals").state == "0"


async def test_backfill_across_both_bst_changes(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    freezer.move_to(now)
    # Covers the 23-hour day (29 March) and the 25-hour day (25 October).
    readings = make_readings(datetime(2026, 3, 20, tzinfo=UTC), now - LAG, seed=3)
    entry = make_entry(["23:30-05:30", "13:00-16:00"], backfill_days=230)

    await setup_entry(hass, entry, FakeEonNext(readings))

    stats = await stored_statistics(hass)
    assert stats == reference_statistics(readings, [(time(23, 30), time(5, 30)), (time(13), time(16))])
    hours_on = {d: sum(1 for h, _, _ in stats["total"] if h.astimezone(LONDON).date() == d) for d in (date(2026, 3, 29), date(2026, 10, 25))}
    assert hours_on == {date(2026, 3, 29): 23, date(2026, 10, 25): 25}


async def test_late_readings_fix_themselves_whatever_order_they_arrive(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    final = make_readings(NOW - timedelta(days=10), NOW + timedelta(days=2) - LAG, seed=7)
    rng = random.Random(7)
    # Everything older than the re-fetch window is present at setup; newer readings trickle in out of order.
    early = {s: v for s, v in final.items() if s < NOW - timedelta(days=2)}
    late = [s for s in final if s not in early]
    rng.shuffle(late)
    fake = FakeEonNext(early)
    entry = make_entry(["00:00-07:00"], backfill_days=10)
    await setup_entry(hass, entry, fake)

    for step in range(1, 13):
        frozen.tick(timedelta(hours=4))
        now = datetime.now(UTC)
        # Random order, but every reading turns up before it falls out of the 3-day re-fetch window.
        arrived = [s for s in late[: len(late) * step // 12] if s < now - timedelta(hours=6)]
        arrived += [s for s in late if s < now - timedelta(days=2)]
        fake.readings.update({s: final[s] for s in arrived})
        await refresh(hass, entry)
    fake.readings = dict(final)
    await refresh(hass, entry)

    assert await stored_statistics(hass) == reference_statistics(final, NIGHT)


async def test_corrected_reading_inside_window_updates_later_totals(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    readings = make_readings(NOW - timedelta(days=10), NOW - LAG, seed=2)
    fake = FakeEonNext(readings)
    entry = make_entry(backfill_days=10)
    await setup_entry(hass, entry, fake)

    corrected = NOW - timedelta(days=2, hours=5)
    fake.readings[corrected] += 1.5
    frozen.tick(timedelta(hours=4))
    await refresh(hass, entry)

    stats = await stored_statistics(hass)
    assert stats == reference_statistics(fake.readings)
    assert stats["total"][-1][2] == pytest.approx(sum(readings.values()) + 1.5)


async def test_regular_sync_continues_from_the_last_hour_before_the_window(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    readings = make_readings(NOW - timedelta(days=10), NOW - LAG, seed=4)
    fake = FakeEonNext(readings)
    entry = make_entry(backfill_days=10)
    await setup_entry(hass, entry, fake)
    fake.calls.clear()

    frozen.tick(timedelta(hours=4))
    await refresh(hass, entry)

    # One request covering the diagnostic week, which includes the 3-day re-fetch window.
    assert fake.calls == [(NOW + timedelta(hours=4) - timedelta(days=7), NOW + timedelta(hours=4))]
    assert await stored_statistics(hass) == reference_statistics(readings)


async def test_interrupted_backfill_resumes_after_restart(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory, hass_storage: dict[str, Any]
) -> None:
    readings = make_readings(NOW - timedelta(days=30), NOW - LAG, seed=5)
    fake = FakeEonNext(readings)
    fake.fail_calls = {3: connection_error()}
    entry = make_entry(["00:00-07:00"], backfill_days=30)

    await setup_entry(hass, entry, fake)

    assert sensor_state(hass, "sync_status").state == "error"
    assert await stored_statistics(hass) == {"total": [], "peak": [], "offpeak": []}
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["backfill"]
    assert saved["cursor"] == (HOUR_START - timedelta(days=14)).isoformat()
    assert len(saved["readings"]) == 14 * 48 - 24 * 2  # the newest day has not been published yet

    # Restart Home Assistant's view of the entry part-way through; the next sync picks up at the cursor.
    await hass.config_entries.async_unload(entry.entry_id)
    frozen.tick(timedelta(minutes=10))
    fake.calls.clear()
    fake.fail_calls = {}
    await setup_entry(hass, entry, fake)

    assert fake.calls[:3] == [
        (HOUR_START - timedelta(days=21), HOUR_START - timedelta(days=14)),
        (HOUR_START - timedelta(days=28), HOUR_START - timedelta(days=21)),
        (HOUR_START - timedelta(days=30), HOUR_START - timedelta(days=28)),
    ]
    assert await stored_statistics(hass) == reference_statistics(readings, NIGHT)
    assert sensor_state(hass, "sync_status").state == "ok"
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["backfill"] is None


async def test_backfill_service_reimports_a_range_and_shifts_later_totals(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    readings = make_readings(NOW - timedelta(days=10), NOW - LAG, seed=6)
    # A gap that E.ON fills in only after it has dropped out of the re-fetch window.
    gap = [s for s in readings if datetime(2026, 1, 13, 18, tzinfo=UTC) <= s < datetime(2026, 1, 14, 2, tzinfo=UTC)]
    fake = FakeEonNext({s: v for s, v in readings.items() if s not in gap})
    entry = make_entry(["00:00-07:00"], backfill_days=10)
    await setup_entry(hass, entry, fake)
    assert await stored_statistics(hass) == reference_statistics(fake.readings, NIGHT)

    fake.readings = dict(readings)
    fake.readings[datetime(2026, 1, 13, 9, tzinfo=UTC)] += 0.25
    frozen.tick(timedelta(hours=1))
    await hass.services.async_call(
        DOMAIN, SERVICE_BACKFILL, {"start_date": "2026-01-13", "end_date": "2026-01-14"}, blocking=True
    )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    assert await stored_statistics(hass) == reference_statistics(fake.readings, NIGHT)


async def test_missing_intervals_sensor_lists_gaps_in_the_last_week(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    readings = make_readings(NOW - timedelta(days=10), NOW - LAG, seed=8)
    gaps = [datetime(2026, 1, 16, 3, tzinfo=UTC), datetime(2026, 1, 16, 3, 30, tzinfo=UTC), datetime(2026, 1, 18, 20, tzinfo=UTC)]
    stale_gap = datetime(2026, 1, 11, 12, tzinfo=UTC)  # older than a week: not reported
    for slot in [*gaps, stale_gap]:
        del readings[slot]
    await setup_entry(hass, make_entry(backfill_days=10), FakeEonNext(readings))

    state = sensor_state(hass, "missing_intervals")
    assert state.state == "3"
    assert state.attributes["missing"] == [
        "2026-01-16T03:00:00+00:00/2026-01-16T04:00:00+00:00",
        "2026-01-18T20:00:00+00:00/2026-01-18T20:30:00+00:00",
    ]


async def test_rejected_credentials_start_reauth(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    fake = FakeEonNext(make_readings(NOW - timedelta(days=2), NOW - LAG))
    fake.fail_calls = {1: EonNextAuthError("Invalid data.", "KT-CT-1138")}
    entry = make_entry(backfill_days=2)

    await setup_entry(hass, entry, fake)

    assert sensor_state(hass, "sync_status").state == "reauth_required"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_changing_offpeak_windows_triggers_a_sync(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    readings = make_readings(NOW - timedelta(days=5), NOW - LAG, seed=9)
    fake = FakeEonNext(readings)
    entry = make_entry(backfill_days=5)
    await setup_entry(hass, entry, fake)
    calls = len(fake.calls)

    hass.config_entries.async_update_entry(entry, options={"offpeak_windows": ["00:00-07:00"]})
    await hass.async_block_till_done(wait_background_tasks=True)
    await refresh(hass, entry)  # flush the debounced refresh
    assert len(fake.calls) > calls

    stats = await stored_statistics(hass)
    recent = [row for row in stats["offpeak"] if row[0] >= NOW - timedelta(days=3)]
    expected = [row for row in reference_statistics(readings, NIGHT)["offpeak"] if row[0] >= NOW - timedelta(days=3)]
    assert [r[1] for r in recent] == [r[1] for r in expected]

    # A refresh-token update must not trigger another sync.
    calls = len(fake.calls)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "refresh_token": "refresh-2"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(fake.calls) == calls

