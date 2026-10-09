"""Diagnostic sensors describing the state of the import."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import STATUSES, EonNextHHConfigEntry, EonNextHHCoordinator

MAX_LISTED_RANGES = 50


async def async_setup_entry(
    hass: HomeAssistant, entry: EonNextHHConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [LastDataSensor(coordinator), SyncStatusSensor(coordinator), MissingIntervalsSensor(coordinator)]
    )


class EonNextHHSensor(CoordinatorEntity[EonNextHHCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EonNextHHCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.mpan}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.mpan)},
            name=f"E.ON Next {coordinator.mpan}",
            manufacturer="E.ON Next",
            model="Electricity meter",
            serial_number=next(iter(coordinator.meter_point.serial_numbers), None),
        )


class LastDataSensor(EonNextHHSensor):
    """End of the most recent half-hour E.ON Next has supplied."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: EonNextHHCoordinator) -> None:
        super().__init__(coordinator, "last_data")

    @property
    def available(self) -> bool:
        return self.coordinator.last_data is not None

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_data


class SyncStatusSensor(EonNextHHSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES

    def __init__(self, coordinator: EonNextHHCoordinator) -> None:
        super().__init__(coordinator, "sync_status")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "detail": self.coordinator.status_detail,
            "last_success": self.coordinator.last_success,
            "backfill_progress": self.coordinator.backfill_progress,
        }


class MissingIntervalsSensor(EonNextHHSensor):
    """Half-hours absent from the last 7 days, up to the latest reading received."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "intervals"

    def __init__(self, coordinator: EonNextHHCoordinator) -> None:
        super().__init__(coordinator, "missing_intervals")

    @property
    def native_value(self) -> int | None:
        return len(self.coordinator.data.missing) if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ranges = self.coordinator.missing_ranges()
        return {
            "missing": [f"{start.isoformat()}/{end.isoformat()}" for start, end in ranges[:MAX_LISTED_RANGES]],
            "truncated": len(ranges) > MAX_LISTED_RANGES,
        }
