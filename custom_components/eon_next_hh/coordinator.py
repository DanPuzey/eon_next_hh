"""Keeps the external statistics for one meter in step with E.ON Next."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import stats
from .api import EonNextAuthError, EonNextClient, EonNextError, MeterPoint, Reading
from .buckets import (
    HOUR,
    SLOT,
    Window,
    compress_ranges,
    floor_hour,
    floor_slot,
    hourly_buckets,
    missing_slots,
    parse_windows,
    running_totals,
)
from .const import (
    BACKFILL_REQUEST_DELAY,
    CONF_ACCOUNT_NUMBER,
    CONF_BACKFILL_DAYS,
    CONF_MPAN,
    CONF_OFFPEAK_WINDOWS,
    CONF_PROPERTY_ID,
    CONF_SERIAL,
    DEFAULT_BACKFILL_DAYS,
    DIAGNOSTIC_WINDOW,
    DOMAIN,
    FETCH_CHUNK,
    REFETCH_WINDOW,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

STATUS_PENDING = "pending"
STATUS_BACKFILLING = "backfilling"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_REAUTH = "reauth_required"
STATUSES = [STATUS_PENDING, STATUS_BACKFILLING, STATUS_OK, STATUS_ERROR, STATUS_REAUTH]

type EonNextHHConfigEntry = ConfigEntry[EonNextHHCoordinator]


def state_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Backfill progress and the last data timestamp, kept across restarts."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")


@dataclass
class SyncResult:
    last_data: datetime | None = None
    missing: list[datetime] = field(default_factory=list)


class EonNextHHCoordinator(DataUpdateCoordinator[SyncResult]):
    config_entry: EonNextHHConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EonNextHHConfigEntry, client: EonNextClient) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.data[CONF_MPAN]}", update_interval=UPDATE_INTERVAL
        )
        self.client = client
        self.meter_point = MeterPoint(
            account_number=entry.data[CONF_ACCOUNT_NUMBER],
            property_id=entry.data[CONF_PROPERTY_ID],
            mpan=entry.data[CONF_MPAN],
            serial_numbers=(entry.data[CONF_SERIAL],) if entry.data.get(CONF_SERIAL) else (),
        )
        self.mpan = self.meter_point.mpan
        self.request_delay = BACKFILL_REQUEST_DELAY
        self.status = STATUS_PENDING
        self.status_detail: str | None = None
        self.last_success: datetime | None = None
        self.backfill_progress: int | None = None
        self.last_data: datetime | None = None
        self._store = state_store(hass, entry.entry_id)
        self._state: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def windows(self) -> list[Window]:
        return parse_windows(self.config_entry.options.get(CONF_OFFPEAK_WINDOWS, []))

    @property
    def backfill_complete(self) -> bool:
        return bool(self._state.get("backfill_complete"))

    async def async_load(self) -> None:
        self._state = await self._store.async_load() or {}
        if last := self._state.get("last_data"):
            self.last_data = datetime.fromisoformat(last)

    async def _async_update_data(self) -> SyncResult:
        async with self._lock:
            return await self._guarded(self._sync)

    async def async_backfill_range(self, start: datetime, end: datetime) -> None:
        """Re-import [start, end), then carry any change in the running totals through to later hours."""
        if not self.backfill_complete:
            raise HomeAssistantError(f"The initial backfill for {self.mpan} is still running")
        try:
            async with self._lock:
                await self._guarded(lambda: self._reimport_range(start, end))
        except ConfigEntryAuthFailed:
            self.config_entry.async_start_reauth(self.hass)
            raise
        finally:
            self.async_update_listeners()

    async def _guarded[T](self, work: Callable[[], Awaitable[T]]) -> T:
        try:
            result = await work()
        except EonNextAuthError as err:
            self._set_status(STATUS_REAUTH, str(err))
            raise ConfigEntryAuthFailed(f"E.ON Next rejected the stored credentials: {err}") from err
        except EonNextError as err:
            self._set_status(STATUS_ERROR, str(err))
            raise UpdateFailed(f"E.ON Next request failed: {err}") from err
        self._set_status(STATUS_OK)
        self.last_success = dt_util.utcnow()
        return result

    def _set_status(self, status: str, detail: str | None = None) -> None:
        self.status = status
        self.status_detail = detail
        if status != STATUS_BACKFILLING:
            self.backfill_progress = None

    async def _sync(self) -> SyncResult:
        if not self.backfill_complete:
            await self._initial_backfill()
        return await self._sync_recent()

    async def _initial_backfill(self) -> None:
        """Page backwards from now, saving each chunk so a restart resumes where it stopped, then import it all."""
        backfill = self._state.get("backfill")
        if backfill is None:
            end = floor_hour(dt_util.utcnow())
            days = self.config_entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
            start = end - timedelta(days=days)
            backfill = {"start": start.isoformat(), "end": end.isoformat(), "cursor": end.isoformat(), "readings": {}}
            self._state["backfill"] = backfill
            await self._store.async_save(self._state)
        start = datetime.fromisoformat(backfill["start"])
        end = datetime.fromisoformat(backfill["end"])
        cursor = datetime.fromisoformat(backfill["cursor"])

        self._set_status(STATUS_BACKFILLING)
        while cursor > start:
            self.backfill_progress = round(100 * (end - cursor) / (end - start))
            self.async_update_listeners()
            chunk_start = max(start, cursor - FETCH_CHUNK)
            _LOGGER.debug("Backfilling %s from %s to %s", self.mpan, chunk_start, cursor)
            readings = await self.client.half_hourly_readings(self.meter_point, chunk_start, cursor)
            backfill["readings"].update({r.start.isoformat(): r.kwh for r in readings})
            cursor = chunk_start
            backfill["cursor"] = cursor.isoformat()
            await self._store.async_save(self._state)
            if cursor > start:
                await asyncio.sleep(self.request_delay)

        readings = [(datetime.fromisoformat(k), v) for k, v in backfill["readings"].items()]
        _, base = await stats.async_base_before(self.hass, self.mpan, start)
        stats.import_rows(self.hass, self.mpan, running_totals(hourly_buckets(readings, self.windows), base))
        await stats.async_flush(self.hass)
        self._state["backfill"] = None
        self._state["backfill_complete"] = True
        await self._store.async_save(self._state)
        _LOGGER.info("Imported %d half-hours of history for %s", len(readings), self.mpan)

    async def _sync_recent(self) -> SyncResult:
        """Continue the running totals from the last hour before the re-fetch window and re-import everything after it."""
        now = dt_util.utcnow()
        cutoff = floor_hour(now - REFETCH_WINDOW)
        base_hour, base = await stats.async_base_before(self.hass, self.mpan, cutoff)
        import_from = base_hour + HOUR if base_hour else cutoff
        diagnostic_from = floor_slot(now - DIAGNOSTIC_WINDOW)

        readings = await self._fetch(min(import_from, diagnostic_from), now)
        buckets = {h: v for h, v in hourly_buckets(_pairs(readings), self.windows).items() if h >= import_from}
        stats.import_rows(self.hass, self.mpan, running_totals(buckets, base))
        await stats.async_flush(self.hass)

        if readings:
            self.last_data = max(self.last_data or readings[-1].start + SLOT, readings[-1].start + SLOT)
            self._state["last_data"] = self.last_data.isoformat()
            await self._store.async_save(self._state)
        recent = [r.start for r in readings if r.start >= diagnostic_from]
        missing = missing_slots(recent, diagnostic_from, max(recent) + SLOT) if recent else []
        return SyncResult(last_data=self.last_data, missing=missing)

    async def _reimport_range(self, start: datetime, end: datetime) -> None:
        end = min(end, dt_util.utcnow())
        _, base = await stats.async_base_before(self.hass, self.mpan, start)
        readings = await self._fetch(start, end)
        rows = running_totals(hourly_buckets(_pairs(readings), self.windows), base)
        # Rebuild later running totals from their stored hourly values rather than re-fetching them.
        later = await stats.async_hourly_states_from(self.hass, self.mpan, end)
        rows += running_totals(later, rows[-1].sum if rows else base)
        stats.import_rows(self.hass, self.mpan, rows)
        await stats.async_flush(self.hass)
        _LOGGER.info("Re-imported %d half-hours for %s between %s and %s", len(readings), self.mpan, start, end)

    async def _fetch(self, start: datetime, end: datetime) -> list[Reading]:
        readings: list[Reading] = []
        chunk_start = start
        while chunk_start < end:
            if chunk_start > start:
                await asyncio.sleep(self.request_delay)
            chunk_end = min(end, chunk_start + FETCH_CHUNK)
            readings += await self.client.half_hourly_readings(self.meter_point, chunk_start, chunk_end)
            chunk_start = chunk_end
        return readings

    def missing_ranges(self) -> list[tuple[datetime, datetime]]:
        return compress_ranges(self.data.missing) if self.data else []


def _pairs(readings: list[Reading]) -> list[tuple[datetime, float]]:
    return [(r.start, r.kwh) for r in readings]
