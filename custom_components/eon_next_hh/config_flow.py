"""Config, options and reauth flows."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EonNextAuthError, EonNextClient, EonNextError, MeterPoint
from .buckets import parse_window
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BACKFILL_DAYS,
    CONF_MPAN,
    CONF_OFFPEAK_WINDOWS,
    CONF_PROPERTY_ID,
    CONF_REFRESH_TOKEN,
    CONF_SERIAL,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
        vol.Required(CONF_BACKFILL_DAYS, default=DEFAULT_BACKFILL_DAYS): NumberSelector(
            NumberSelectorConfig(min=1, max=1095, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="days")
        ),
    }
)


class EonNextHHConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}
        self._refresh_token = ""
        self._meter_points: list[MeterPoint] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EonNextHHOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = EonNextClient(async_get_clientsession(self.hass))
            try:
                await client.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
                meter_points = await client.all_meter_points()
            except EonNextAuthError:
                errors["base"] = "invalid_auth"
            except EonNextError as err:
                _LOGGER.warning("Could not reach E.ON Next: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not meter_points:
                    return self.async_abort(reason="no_meters")
                self._user_input = user_input
                self._refresh_token = client.refresh_token or ""
                self._meter_points = meter_points
                if len(meter_points) == 1:
                    return await self._async_create(meter_points[0])
                return await self.async_step_meter()
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
            ),
            errors=errors,
        )

    async def async_step_meter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            chosen = next(mp for mp in self._meter_points if mp.mpan == user_input[CONF_MPAN])
            return await self._async_create(chosen)
        options = [
            SelectOptionDict(
                value=mp.mpan,
                label=f"MPAN {mp.mpan} (meter {', '.join(mp.serial_numbers) or 'unknown'}, account {mp.account_number})",
            )
            for mp in self._meter_points
        ]
        return self.async_show_form(
            step_id="meter",
            data_schema=vol.Schema({vol.Required(CONF_MPAN): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def _async_create(self, meter_point: MeterPoint) -> ConfigFlowResult:
        await self.async_set_unique_id(meter_point.mpan)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"E.ON Next {meter_point.mpan}",
            data={
                CONF_EMAIL: self._user_input[CONF_EMAIL],
                CONF_PASSWORD: self._user_input[CONF_PASSWORD],
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_ACCOUNT_NUMBER: meter_point.account_number,
                CONF_PROPERTY_ID: meter_point.property_id,
                CONF_MPAN: meter_point.mpan,
                CONF_SERIAL: meter_point.serial_numbers[0] if meter_point.serial_numbers else None,
                CONF_BACKFILL_DAYS: int(self._user_input[CONF_BACKFILL_DAYS]),
            },
            options={CONF_OFFPEAK_WINDOWS: []},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = EonNextClient(async_get_clientsession(self.hass))
            try:
                await client.login(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            except EonNextAuthError:
                errors["base"] = "invalid_auth"
            except EonNextError as err:
                _LOGGER.warning("Could not reach E.ON Next: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD], CONF_REFRESH_TOKEN: client.refresh_token},
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    )
                }
            ),
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )


class EonNextHHOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"window": ""}
        if user_input is not None:
            raw = [w.strip() for w in user_input.get(CONF_OFFPEAK_WINDOWS, []) if w.strip()]
            try:
                windows = [str(parse_window(w)) for w in raw]
            except ValueError as err:
                errors[CONF_OFFPEAK_WINDOWS] = "invalid_window"
                placeholders["window"] = str(err)
            else:
                return self.async_create_entry(data={CONF_OFFPEAK_WINDOWS: windows})
        current = user_input or self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_OFFPEAK_WINDOWS, default=list(current.get(CONF_OFFPEAK_WINDOWS, []))): TextSelector(
                        TextSelectorConfig(multiple=True)
                    )
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )
