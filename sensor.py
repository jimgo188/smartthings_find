"""Battery and last-update sensors for Samsung Find (China).

Both values come from Samsung's stored operations:

* battery  -> the ``CHECK_CONNECTION`` operation (only updated when the device
              answers a connection check, so it can lag behind)
* last update -> ``extra.gpsUtcDt`` of the newest location operation
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .utils import DATA_COORDINATOR, DATA_DEVICES, get_battery_level

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-device sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data[DATA_COORDINATOR]
    entities: list[SensorEntity] = []
    for device in data[DATA_DEVICES]:
        entities.append(BatterySensor(coordinator, device))
        entities.append(LastUpdateSensor(coordinator, device))
    async_add_entities(entities)


class _DeviceSensor(SensorEntity):
    """Common base: one sensor of one Samsung Find device."""

    _attr_should_poll = False

    def __init__(
        self, coordinator: Any, device: dict[str, Any], label: str, key: str
    ) -> None:
        """Initialise the sensor."""
        self.coordinator = coordinator
        self._device = device["data"]
        self._dvce_id = self._device["dvceID"]
        name = self._device.get("modelName") or self._dvce_id
        self._attr_name = f"{name} {label}"
        self._attr_unique_id = f"stf_{key}_{self._dvce_id}"
        self._attr_device_info = device["ha_dev_info"]

    @property
    def _state_data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._dvce_id) or {}

    @property
    def available(self) -> bool:
        return bool(self._state_data.get("update_success"))

    async def async_added_to_hass(self) -> None:
        """Update whenever the coordinator fetches new data."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )


class BatterySensor(_DeviceSensor):
    """Battery percentage, with the time it was measured."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery"

    def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
        """Initialise the battery sensor."""
        super().__init__(coordinator, device, "电量", "battery")

    @property
    def native_value(self) -> int | None:
        """Return the battery percentage."""
        return get_battery_level(self._attr_name, self._state_data.get("ops"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose when the battery value was measured."""
        measured = self._state_data.get("battery_updated")
        return {
            "measured_at": measured.isoformat() if measured else None,
            "connected": (self._state_data.get("connection") or {}).get("connected"),
        }


class LastUpdateSensor(_DeviceSensor):
    """When the device last reported its position to Samsung."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
        """Initialise the last-update sensor."""
        super().__init__(coordinator, device, "最后更新", "last_update")

    @property
    def native_value(self) -> datetime | None:
        """Return the timestamp of the newest location operation."""
        location = self._state_data.get("used_loc") or {}
        return location.get("gps_date")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the poll time and the location source."""
        data = self._state_data
        location = data.get("used_loc") or {}
        polled_at = data.get("polled_at")
        return {
            "location_source": location.get("oprn_type"),
            "polled_at": polled_at.isoformat() if polled_at else None,
            "requested_refresh": data.get("requested"),
            "request_code": data.get("request_code"),
        }
