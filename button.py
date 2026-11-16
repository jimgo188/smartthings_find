"""Buttons: refresh the location and ring the device.

Ringing is the same operation the website uses
(``addOperation`` with ``operation: "RING"``); Samsung refuses it for SmartTags
(``menu.ringStop`` is missing) and needs a nearby Galaxy device to relay it.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
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
    """Set up the per-device buttons."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data[DATA_COORDINATOR]
    entities: list[ButtonEntity] = []
    for device in data[DATA_DEVICES]:
        entities.append(RefreshLocationButton(coordinator, device))
        entities.append(RingButton(coordinator, device, start=True))
        entities.append(RingButton(coordinator, device, start=False))
    async_add_entities(entities)


class _DeviceButton(ButtonEntity):
    """Common base for the buttons."""

    _attr_should_poll = False

    def __init__(
        self, coordinator: Any, device: dict[str, Any], label: str, key: str
    ) -> None:
        """Initialise the button."""
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
        """Unavailable while the last update of this device failed."""
        return bool(self._state_data.get("update_success"))

    def _feature(self, feature: str) -> bool:
        """Whether Samsung offers this feature for the device (default: yes)."""
        menu = self._state_data.get("menu") or {}
        return menu.get(feature, True)


class RefreshLocationButton(_DeviceButton):
    """Force Samsung Find to report a fresh position and battery for a device."""

    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, coordinator: Any, device: dict[str, Any]) -> None:
        """Initialise the refresh button."""
        super().__init__(coordinator, device, "立即刷新位置", "refresh_location")

    async def async_press(self) -> None:
        """Request a fresh position and wait for it."""
        _LOGGER.debug("Manual location refresh requested for %s", self._attr_name)
        await self.coordinator.async_refresh_device(self._dvce_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose when the device last reported, so the result is visible."""
        entry = self._state_data
        location = entry.get("used_loc") or {}
        gps_date = location.get("gps_date")
        polled_at = entry.get("polled_at")
        return {
            "device_id": self._dvce_id,
            "last_seen": gps_date.isoformat() if gps_date else None,
            "last_update": polled_at.isoformat() if polled_at else None,
            "location_found": bool(location),
            "request_code": entry.get("request_code"),
            "last_error": entry.get("error"),
        }


class RingButton(_DeviceButton):
    """Start or stop ringing the device."""

    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator: Any, device: dict[str, Any], *, start: bool) -> None:
        """Initialise the ring button."""
        self._start = start
        label = "响铃" if start else "停止响铃"
        key = "ring" if start else "stop_ring"
        super().__init__(coordinator, device, label, key)
        self._attr_icon = "mdi:bell-ring" if start else "mdi:bell-off"

    @property
    def available(self) -> bool:
        """Hidden as unavailable when the device cannot do it."""
        return super().available and self._feature(
            "ring" if self._start else "ringStop"
        )

    async def async_press(self) -> None:
        """Send the ring request."""
        _LOGGER.debug(
            "%s: %s", self._attr_name, "ring" if self._start else "stop ringing"
        )
        await self.coordinator.async_ring(self._dvce_id, self._start)
