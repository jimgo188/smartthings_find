"""Device tracker entities for Samsung Find (China)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .utils import DATA_COORDINATOR, DATA_DEVICES, get_battery_level, get_sub_location

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one device tracker per Samsung Find device."""
    data = hass.data[DOMAIN][entry.entry_id]
    devices = data[DATA_DEVICES]
    coordinator = data[DATA_COORDINATOR]

    entities: list[SmartThingsDeviceTracker] = []
    for device in devices:
        # Galaxy Buds report the two earbuds separately
        if device["data"].get("subType") == "CANAL2":
            entities.append(SmartThingsDeviceTracker(coordinator, device, "left"))
            entities.append(SmartThingsDeviceTracker(coordinator, device, "right"))
        entities.append(SmartThingsDeviceTracker(coordinator, device))
    async_add_entities(entities)


class SmartThingsDeviceTracker(TrackerEntity):
    """A Samsung Find device, tracking its last known GPS position."""

    _attr_should_poll = False
    _attr_source_type = SourceType.GPS

    def __init__(
        self,
        coordinator: Any,
        device: dict[str, Any],
        sub_device_name: str | None = None,
    ) -> None:
        """Initialise the tracker entity."""
        self.coordinator = coordinator
        self._device = device["data"]
        self._dvce_id = self._device["dvceID"]
        self._sub_device_name = sub_device_name

        unique_id = f"stf_device_tracker_{self._dvce_id}"
        if sub_device_name:
            unique_id = f"{unique_id}_{sub_device_name}"
        self._attr_unique_id = unique_id

        name = self._device.get("modelName") or self._dvce_id
        if sub_device_name:
            name = f"{name} {sub_device_name.capitalize()}"
        self._attr_name = name
        self._attr_device_info = device["ha_dev_info"]

        icons = self._device.get("icons") or {}
        if icons.get("coloredIcon"):
            self._attr_entity_picture = icons["coloredIcon"]

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator and clean up on removal."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def _state_data(self) -> dict[str, Any]:
        """Return the coordinator payload of this device (never None)."""
        return self.coordinator.data.get(self._dvce_id) or {}

    @property
    def available(self) -> bool:
        """Return True when the last update of this device worked."""
        return bool(self._state_data.get("update_success"))

    def _raw_location(self) -> dict[str, Any]:
        """Return the stored location of this device / sub device."""
        data = self._state_data
        if self._sub_device_name:
            _, sub_loc = get_sub_location(data.get("ops"), self._sub_device_name)
            return sub_loc or {}
        return data.get("used_loc") or {}

    @property
    def _max_age_minutes(self) -> int:
        return getattr(self.coordinator, "max_location_age", 0) or 0

    def _is_expired(self, location: dict[str, Any]) -> bool:
        """True when the position is older than the configured maximum age."""
        max_age = self._max_age_minutes
        if not max_age or not location:
            return False
        gps_date = location.get("gps_date")
        if not gps_date:
            return False
        age = (datetime.now(timezone.utc) - gps_date).total_seconds() / 60
        return age > max_age

    def _location(self) -> dict[str, Any]:
        """Location used for the coordinates (empty when it is too old)."""
        location = self._raw_location()
        return {} if self._is_expired(location) else location

    @property
    def latitude(self) -> float | None:
        """Return the latitude of the device."""
        return self._location().get("latitude")

    @property
    def longitude(self) -> float | None:
        """Return the longitude of the device."""
        return self._location().get("longitude")

    @property
    def location_accuracy(self) -> float:
        """Return the accuracy in meters (0 when unknown)."""
        accuracy = self._location().get("gps_accuracy")
        return accuracy if accuracy is not None else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra information about the device."""
        data = self._state_data
        raw_location = self._raw_location()
        location = self._location()
        gps_date = raw_location.get("gps_date")
        polled_at = data.get("polled_at")

        attributes: dict[str, Any] = {
            "device_id": self._dvce_id,
            "device_type": self._device.get("deviceType"),
            "model": self._device.get("modelID"),
            "nickname": self._device.get("nickName"),
            "update_success": bool(data.get("update_success")),
            "location_found": bool(location),
            # 位置超过「最大有效期」时，坐标会被隐藏（不再当作当前位置）
            "position_expired": self._is_expired(raw_location),
            "location_source": location.get("oprn_type"),
            # 三星是否为这台设备允许上报位置（需在网页上同意条款）
            "location_allowed": data.get("location_allowed"),
            # 账号级条款状态（Y = 已同意）
            "agreements": getattr(self.coordinator, "account_flags", {}),
            # 三星那边的数据时间（设备上报的时刻）
            "last_seen": gps_date.isoformat() if gps_date else None,
            # 集成最后一次成功采集的时间
            "last_update": polled_at.isoformat() if polled_at else None,
            "requested_refresh": data.get("requested"),
            "request_code": data.get("request_code"),
            "connected": data.get("connected"),
            "last_error": data.get("error"),
        }
        battery_updated = data.get("battery_updated")
        attributes["battery_last_seen"] = (
            battery_updated.isoformat() if battery_updated else None
        )
        if not self._sub_device_name:
            attributes["battery_level"] = get_battery_level(
                self.name or self._dvce_id, data.get("ops")
            )
        return attributes
