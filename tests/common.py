"""Helpers for integration tests: a fake E.ON Next client, entry setup and an independent reference calculation."""

from __future__ import annotations

import random
from datetime import UTC, datetime, time, timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.eon_next_hh.api import EonNextError, MeterPoint, Reading
from custom_components.eon_next_hh.buckets import LONDON
from custom_components.eon_next_hh.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BACKFILL_DAYS,
    CONF_MPAN,
    CONF_OFFPEAK_WINDOWS,
    CONF_PROPERTY_ID,
    CONF_REFRESH_TOKEN,
    CONF_SERIAL,
    DOMAIN,
)

MPAN = "1900000000001"
SLOT = timedelta(minutes=30)
METER = MeterPoint("A-1234ABCD", "1234567", MPAN, ("21E0000001",))
STAT_IDS = {series: f"{DOMAIN}:{MPAN}_{series}" for series in ("total", "peak", "offpeak")}


class FakeEonNext:
    """Serves whatever readings the test has put in `readings`, recording each request."""

    def __init__(self, readings: dict[datetime, float] | None = None) -> None:
        self.readings: dict[datetime, float] = dict(readings or {})
        self.calls: list[tuple[datetime, datetime]] = []
        self.fail_calls: dict[int, Exception] = {}
        self.refresh_token = "refresh-1"

    async def half_hourly_readings(self, meter_point: MeterPoint, start: datetime, end: datetime) -> list[Reading]:
        assert meter_point == METER
        self.calls.append((start, end))
        if error := self.fail_calls.get(len(self.calls)):
            raise error
        return [Reading(s, kwh) for s, kwh in sorted(self.readings.items()) if start <= s < end]


def make_readings(start: datetime, end: datetime, seed: int = 0) -> dict[datetime, float]:
    rng = random.Random(seed)
    readings = {}
    slot = start
    while slot < end:
        readings[slot] = round(rng.uniform(0.05, 2.5), 3)
        slot += SLOT
    return readings


def make_entry(windows: list[str] | None = None, backfill_days: int = 10) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=MPAN,
        title=f"E.ON Next {MPAN}",
        data={
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_ACCOUNT_NUMBER: METER.account_number,
            CONF_PROPERTY_ID: METER.property_id,
            CONF_MPAN: MPAN,
            CONF_SERIAL: METER.serial_numbers[0],
            CONF_BACKFILL_DAYS: backfill_days,
        },
        options={CONF_OFFPEAK_WINDOWS: windows or []},
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry, fake: FakeEonNext) -> None:
    """Set up (or reload) the entry against the fake client and wait for the initial sync to finish."""
    with (
        patch("custom_components.eon_next_hh.EonNextClient", return_value=fake),
        patch("custom_components.eon_next_hh.coordinator.BACKFILL_REQUEST_DELAY", 0),
    ):
        if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}:
            entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


async def stored_statistics(hass: HomeAssistant) -> dict[str, list[tuple[datetime, float, float]]]:
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2000, 1, 1, tzinfo=UTC),
        None,
        set(STAT_IDS.values()),
        "hour",
        None,
        {"state", "sum"},
    )
    return {
        series: [(dt_util.utc_from_timestamp(r["start"]), r["state"], r["sum"]) for r in rows.get(stat_id, [])]
        for series, stat_id in STAT_IDS.items()
    }


def reference_statistics(
    readings: dict[datetime, float], windows: list[tuple[time, time]] | None = None
) -> dict[str, list[tuple[datetime, float, float]]]:
    """Expected statistics, computed independently of the integration: local wall-clock classification, UTC hours."""

    def offpeak(slot: datetime) -> bool:
        t = slot.astimezone(LONDON).time()
        return any((a <= t < b) if a < b else (t >= a or t < b) for a, b in windows or [])

    hours: dict[datetime, dict[str, int]] = {}
    for slot, kwh in readings.items():
        micro = round(kwh * 1_000_000)
        hour = hours.setdefault(slot.replace(minute=0), {"total": 0, "peak": 0, "offpeak": 0})
        hour["total"] += micro
        hour["offpeak" if offpeak(slot) else "peak"] += micro
    result: dict[str, list[tuple[datetime, float, float]]] = {}
    for series in ("total", "peak", "offpeak"):
        running = 0
        result[series] = []
        for hour in sorted(hours):
            running += hours[hour][series]
            result[series].append((hour, hours[hour][series] / 1_000_000, running / 1_000_000))
    return result


def sensor_state(hass: HomeAssistant, key: str) -> Any:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{MPAN}_{key}")
    return hass.states.get(entity_id) if entity_id else None


def connection_error() -> Exception:
    return EonNextError("Network error: ClientConnectorError")
