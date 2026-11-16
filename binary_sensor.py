"""Connectivity and lock binary sensors for Samsung Find (China).

Both come from the ``CHECK_CONNECTION`` operation Samsung stores for a device:

  extra.isConnected      -> connectivity
  extra.lockStatus       -> {"normalLock": screen locked, "fmmLock": remote lock}
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .utils import DATA_COORDINATOR, DATA_DEVICES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the per-device binary sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data[DATA_COORDINATOR]
    entities: list[BinarySensorEntity] = []
    for device in data[DATA_DEVICES]:
        entities.append(ConnectedSensor(coordinator, device))
        # Tags have no screen; only phones/tablets/watches answer with a lock
        if device["data"].get("deviceType") != "TAG":
            entities.append(LockSensor(coordinator, device))
    async_add_entities(entities)


class _DeviceBinarySensor(BinarySensorEntity):
    """Common base for the binary sensors."""

    _attr_should_poll = False

    def __init__(self, coordinator: Any, device: dict[str, Any], label: str, key: str) -> None:
        """Initialise the sensor."""
        self.coordinator = coordinator
        self._device = device["data"]
        self._dvce_id = self._device["dvceID"]
        name = self._device.get("modelName") or self._dvce_id
        self._attr_name = f"{name} {label}"
        self._attr_unique_id = f"stf_{key}_{self._dvce_id}"
        self._attr_device_info = device["ha_dev_info"]

    @property
    def _connection(self) -> dict[str, Any]:
        return ((self.coordinator.data or {}).get(self._dvce_id) or {}).get(
            "connection"
        ) or {}

    @property
    def available(self) -> bool:
        return bool(((self.coordinator.data or {}).get(self._dvce_id) or {}).get("update_success"))

    async def async_added_to_hass(self) -> None:
        """Update whenever the coordinator fetches new data."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )


class ConnectedSensor(_DeviceBinarySensor):
    """Whether the device answered its last connection check."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
        """Initialise the connectivity sensor."""
        super().__init__(coordinator, device, "连接状态", "connected")

    @property
    def is_on(self) -> bool | None:
        """Return True when the device is reachable."""
        return self._connection.get("connected")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose when the connection was checked."""
        checked = self._connection.get("checked_at")
        return {"checked_at": checked.isoformat() if checked else None}


class LockSensor(_DeviceBinarySensor):
    """Screen lock at the last connection check (on = unlocked)."""

    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
        """Initialise the lock sensor."""
        super().__init__(coordinator, device, "锁屏状态", "lock")

    @property
    def is_on(self) -> bool | None:
        """Locked -> False, unlocked -> True (HA's LOCK device class)."""
        locked = self._connection.get("lock_locked")
        if locked is None:
            return None
        return not locked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the remote (Find) lock and the check time."""
        connection = self._connection
        checked = connection.get("checked_at")
        return {
            "screen_locked": connection.get("lock_locked"),
            "remote_locked": connection.get("lock_remote"),
            "checked_at": checked.isoformat() if checked else None,
        }
