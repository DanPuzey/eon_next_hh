"""Shared fixtures."""

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: Any, enable_custom_integrations: None) -> None:
    """The integration depends on the recorder, which must be set up before hass."""
