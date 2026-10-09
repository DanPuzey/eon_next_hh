"""Config, reauth and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.eon_next_hh.api import EonNextAuthError, EonNextConnectionError, MeterPoint
from custom_components.eon_next_hh.const import DOMAIN

from .common import METER, MPAN, make_entry

SECOND_METER = MeterPoint("A-1234ABCD", "7654321", "1900000000009", ("21E0000009",))
USER_INPUT = {"email": "me@example.com", "password": "hunter2", "backfill_days": 90}


@pytest.fixture
def client() -> Generator[MagicMock]:
    mock = MagicMock()
    mock.login = AsyncMock(return_value="refresh-1")
    mock.all_meter_points = AsyncMock(return_value=[METER])
    mock.refresh_token = "refresh-1"
    with (
        patch("custom_components.eon_next_hh.config_flow.EonNextClient", return_value=mock),
        patch("custom_components.eon_next_hh.async_setup_entry", return_value=True),
    ):
        yield mock


async def test_single_meter_creates_entry(hass: HomeAssistant, client: MagicMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"E.ON Next {MPAN}"
    assert result["result"].unique_id == MPAN
    assert result["data"] == {
        "email": "me@example.com",
        "password": "hunter2",
        "refresh_token": "refresh-1",
        "account_number": "A-1234ABCD",
        "property_id": "1234567",
        "mpan": MPAN,
        "serial": "21E0000001",
        "backfill_days": 90,
    }
    assert result["options"] == {"offpeak_windows": []}
    client.login.assert_awaited_once_with("me@example.com", "hunter2")


async def test_choose_between_meters(hass: HomeAssistant, client: MagicMock) -> None:
    client.all_meter_points.return_value = [METER, SECOND_METER]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "meter"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mpan": SECOND_METER.mpan})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["mpan"] == SECOND_METER.mpan
    assert result["data"]["property_id"] == "7654321"


@pytest.mark.parametrize(
    ("error", "expected"),
    [(EonNextAuthError("Invalid data.", "KT-CT-1138"), "invalid_auth"), (EonNextConnectionError("HTTP 503"), "cannot_connect")],
)
async def test_login_errors(hass: HomeAssistant, client: MagicMock, error: Exception, expected: str) -> None:
    client.login.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_import_meters(hass: HomeAssistant, client: MagicMock) -> None:
    client.all_meter_points.return_value = []
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_meters"


async def test_meter_already_configured(hass: HomeAssistant, client: MagicMock) -> None:
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_stores_new_password_and_token(hass: HomeAssistant, client: MagicMock) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    client.refresh_token = "refresh-new"

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["email"] == "me@example.com"

    client.login.side_effect = EonNextAuthError("Invalid data.", "KT-CT-1138")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new-password"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-password"
    assert entry.data["refresh_token"] == "refresh-new"
    client.login.assert_awaited_with("me@example.com", "new-password")


async def test_options_flow_validates_and_normalises_windows(hass: HomeAssistant) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"offpeak_windows": ["00:00-07:00", "25:00-01:00"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"offpeak_windows": "invalid_window"}
    assert "25:00-01:00" in result["description_placeholders"]["window"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"offpeak_windows": ["23:30 - 5:30", " ", "13:00-16:00"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"offpeak_windows": ["23:30-05:30", "13:00-16:00"]}
