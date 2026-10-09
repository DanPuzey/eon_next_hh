"""E.ON Next half-hourly consumption, imported into long-term statistics."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import EonNextClient
from .buckets import LONDON
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_END_DATE,
    ATTR_START_DATE,
    CONF_OFFPEAK_WINDOWS,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    SERVICE_BACKFILL,
)
from .coordinator import EonNextHHConfigEntry, EonNextHHCoordinator, state_store

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Required(ATTR_END_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def backfill(call: ServiceCall) -> None:
        start_date: date = call.data[ATTR_START_DATE]
        end_date: date = call.data[ATTR_END_DATE]
        if end_date < start_date:
            raise ServiceValidationError("end_date must not be before start_date")
        entries = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if ATTR_CONFIG_ENTRY_ID not in call.data or entry.entry_id == call.data[ATTR_CONFIG_ENTRY_ID]
        ]
        if not entries:
            raise ServiceValidationError("No loaded E.ON Next half-hourly entry matches")
        start = local_midnight(start_date)
        end = local_midnight(end_date + timedelta(days=1))
        for entry in entries:
            await entry.runtime_data.async_backfill_range(start, end)

    hass.services.async_register(DOMAIN, SERVICE_BACKFILL, backfill, schema=BACKFILL_SCHEMA)
    return True


def local_midnight(day: date) -> datetime:
    return datetime.combine(day, time(), LONDON)


async def async_setup_entry(hass: HomeAssistant, entry: EonNextHHConfigEntry) -> bool:
    @callback
    def save_refresh_token(token: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_REFRESH_TOKEN: token})

    client = EonNextClient(
        async_get_clientsession(hass),
        entry.data.get(CONF_REFRESH_TOKEN),
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        on_refresh_token=save_refresh_token,
    )
    coordinator = EonNextHHCoordinator(hass, entry, client)
    await coordinator.async_load()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    windows = entry.options.get(CONF_OFFPEAK_WINDOWS)

    async def options_updated(hass: HomeAssistant, entry: EonNextHHConfigEntry) -> None:
        # Also fires when a new refresh token is saved; only an off-peak change warrants a sync.
        nonlocal windows
        if entry.options.get(CONF_OFFPEAK_WINDOWS) != windows:
            windows = entry.options.get(CONF_OFFPEAK_WINDOWS)
            await coordinator.async_request_refresh()

    entry.async_on_unload(entry.add_update_listener(options_updated))
    # The first sync may be a backfill lasting minutes, so it must not hold up setup.
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN} initial sync")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EonNextHHConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EonNextHHConfigEntry) -> None:
    await state_store(hass, entry.entry_id).async_remove()
