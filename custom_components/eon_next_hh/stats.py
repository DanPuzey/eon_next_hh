"""Reading and writing the external statistics in the recorder."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .buckets import HOUR, SERIES, HourRow, Sums, from_micro, to_micro
from .const import DOMAIN

SERIES_NAMES = {"total": "total", "peak": "peak", "offpeak": "off-peak"}
LOOKBACK = timedelta(days=14)


def statistic_id(mpan: str, series: str) -> str:
    return f"{DOMAIN}:{mpan}_{series}"


def metadata(mpan: str, series: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"E.ON Next {mpan} {SERIES_NAMES[series]}",
        source=DOMAIN,
        statistic_id=statistic_id(mpan, series),
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


async def async_last_hour_before(hass: HomeAssistant, mpan: str, before: datetime) -> datetime | None:
    """Start of the latest stored hour that starts before `before`."""
    total_id = statistic_id(mpan, "total")
    recorder = get_instance(hass)
    rows = await recorder.async_add_executor_job(
        statistics_during_period, hass, before - LOOKBACK, before, {total_id}, "hour", None, {"sum"}
    )
    if rows.get(total_id):
        return dt_util.utc_from_timestamp(rows[total_id][-1]["start"])
    # Nothing recent (e.g. HA was off for weeks): the latest row overall may still precede `before`.
    last = await recorder.async_add_executor_job(get_last_statistics, hass, 1, total_id, False, {"sum"})
    if last.get(total_id):
        start = dt_util.utc_from_timestamp(last[total_id][0]["start"])
        if start < before:
            return start
    return None


async def async_sums_at(hass: HomeAssistant, mpan: str, hour: datetime) -> Sums:
    """Running totals at the end of the stored hour starting at `hour`."""
    ids = {statistic_id(mpan, s) for s in SERIES}
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, hour, hour + HOUR, ids, "hour", None, {"sum"}
    )
    values = {}
    for series in SERIES:
        series_rows = rows.get(statistic_id(mpan, series)) or [{}]
        values[series] = to_micro(series_rows[0].get("sum") or 0.0)
    return Sums(**values)


async def async_base_before(hass: HomeAssistant, mpan: str, before: datetime) -> tuple[datetime | None, Sums]:
    """The latest stored hour starting before `before`, with the running totals to continue from."""
    hour = await async_last_hour_before(hass, mpan, before)
    if hour is None:
        return None, Sums()
    return hour, await async_sums_at(hass, mpan, hour)


def import_rows(hass: HomeAssistant, mpan: str, rows: list[HourRow]) -> None:
    if not rows:
        return
    for series in SERIES:
        async_add_external_statistics(
            hass,
            metadata(mpan, series),
            [
                StatisticData(start=row.start, state=from_micro(row.state.get(series)), sum=from_micro(row.sum.get(series)))
                for row in rows
            ],
        )


async def async_hourly_states_from(hass: HomeAssistant, mpan: str, start: datetime) -> dict[datetime, Sums]:
    """Stored per-hour consumption for every hour starting at or after `start`."""
    ids = {statistic_id(mpan, s) for s in SERIES}
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, ids, "hour", None, {"state"}
    )
    by_hour: dict[datetime, dict[str, int]] = {}
    for series in SERIES:
        for row in rows.get(statistic_id(mpan, series), []):
            hour = dt_util.utc_from_timestamp(row["start"])
            by_hour.setdefault(hour, dict.fromkeys(SERIES, 0))[series] = to_micro(row["state"] or 0.0)
    return {hour: Sums(**values) for hour, values in by_hour.items()}


async def async_flush(hass: HomeAssistant) -> None:
    """Wait for queued imports to be committed so later reads see them."""
    await get_instance(hass).async_block_till_done()
