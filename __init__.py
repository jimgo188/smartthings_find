"""Samsung Find (China) - GPS device tracker for Home Assistant.

Two ways to authenticate:

* ``samsung_account`` (recommended): one interactive sign-in on Samsung's own
  page yields a long lived ``userauth_token``; Home Assistant uses it to mint
  a new ``JSESSIONID`` whenever Samsung ends the old one.
* ``cookie``: a ``JSESSIONID`` copied from a logged-in browser.  Simple, but
  it has to be replaced by hand when it expires.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import AccountAuthError, async_mint_session
from .const import (
    AUTH_METHOD_ACCOUNT,
    AUTH_METHOD_COOKIE,
    CONF_ACCOUNT,
    CONF_ACTIVE_INTERVAL,
    CONF_ACTIVE_INTERVAL_DEFAULT,
    CONF_ACTIVE_MODE_OTHERS,
    CONF_ACTIVE_MODE_OTHERS_DEFAULT,
    CONF_ACTIVE_MODE_SMARTTAGS,
    CONF_ACTIVE_MODE_SMARTTAGS_DEFAULT,
    CONF_AUTH_METHOD,
    CONF_JSESSIONID,
    CONF_MAX_LOCATION_AGE,
    CONF_MAX_LOCATION_AGE_DEFAULT,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATE_INTERVAL_DEFAULT,
    DOMAIN,
    ISSUE_LOCATION_NOT_ALLOWED,
    SESSION_RENEW_MIN_INTERVAL,
)
from .utils import (
    DATA_COOKIE,
    DATA_COORDINATOR,
    DATA_CSRF,
    DATA_DEVICES,
    DATA_SESSION,
    async_get_account_flags,
    async_request_check,
    async_request_ring,
    create_api_session,
    fetch_csrf,
    get_device_location,
    get_devices,
    normalize_cookie,
)

_LOGGER = logging.getLogger(__name__)

# Only the device tracker and the "refresh now" button are loaded.
PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

#: How long a manual refresh waits for the device to report a new position
REFRESH_POLL_ATTEMPTS = 3
REFRESH_POLL_SECONDS = 8


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def _async_build_cookie(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[str, Callable[[], Awaitable[str]] | None]:
    """Return the current cookie plus a way to renew it (None for cookie mode)."""
    auth_method = entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_COOKIE)
    grant = entry.data.get(CONF_ACCOUNT)

    if auth_method == AUTH_METHOD_ACCOUNT and grant:
        # A dedicated session: the account login is completed through cookies,
        # so it must not share Home Assistant's global cookie jar.
        # Real jar here: the account login is completed through cookies.
        # auto_cleanup=True lets Home Assistant close it on unload.
        auth_session = async_create_clientsession(
            hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
        )

        async def renew() -> str:
            return f"JSESSIONID={await async_mint_session(auth_session, grant)}"

        return await renew(), renew

    return normalize_cookie(entry.data.get(CONF_JSESSIONID, "")), None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Samsung Find from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    entry_id: str = entry.entry_id
    store: dict[str, Any] = hass.data[DOMAIN].setdefault(entry_id, {})

    active_smarttags = entry.options.get(
        CONF_ACTIVE_MODE_SMARTTAGS, CONF_ACTIVE_MODE_SMARTTAGS_DEFAULT
    )
    active_others = entry.options.get(
        CONF_ACTIVE_MODE_OTHERS, CONF_ACTIVE_MODE_OTHERS_DEFAULT
    )
    update_interval = entry.options.get(
        CONF_UPDATE_INTERVAL, CONF_UPDATE_INTERVAL_DEFAULT
    )
    active_interval = entry.options.get(
        CONF_ACTIVE_INTERVAL, CONF_ACTIVE_INTERVAL_DEFAULT
    )
    max_location_age = entry.options.get(
        CONF_MAX_LOCATION_AGE, CONF_MAX_LOCATION_AGE_DEFAULT
    )

    session = create_api_session(hass)
    try:
        cookie, renew = await _async_build_cookie(hass, entry)
        csrf = await fetch_csrf(session, cookie)
        devices = await get_devices(hass, session, cookie, csrf)
    except (ConfigEntryAuthFailed, AccountAuthError) as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001 - network hiccup, retry later
        raise ConfigEntryNotReady(
            f"Cannot reach findmymobile.samsung.cn: {err}"
        ) from err

    store.update(
        {
            DATA_COOKIE: cookie,
            DATA_SESSION: session,
            DATA_CSRF: csrf,
            CONF_ACTIVE_MODE_SMARTTAGS: active_smarttags,
            CONF_ACTIVE_MODE_OTHERS: active_others,
        }
    )
    _LOGGER.debug("Samsung Find: %s device(s) on this account", len(devices))
    if not devices:
        _LOGGER.warning(
            "Samsung Find returned an empty device list - "
            "check the account on findmymobile.samsung.cn"
        )

    account_flags = await async_get_account_flags(session, cookie)

    coordinator = SmartThingsFindCoordinator(
        hass,
        session,
        devices,
        update_interval,
        cookie,
        active_smarttags,
        active_others,
        renew_cookie=renew,
        active_interval=active_interval,
        max_location_age=max_location_age,
    )
    coordinator.account_flags = account_flags
    await coordinator.async_config_entry_first_refresh()

    store.update({DATA_COORDINATOR: coordinator, DATA_DEVICES: devices})

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    else:
        _LOGGER.error("Unload of Samsung Find failed")
    return unload_ok


class SmartThingsFindCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the last known location of every device."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: Any,
        devices: list[dict[str, Any]],
        update_interval: int,
        cookie: str,
        active_smarttags: bool,
        active_others: bool,
        renew_cookie: Callable[[], Awaitable[str]] | None = None,
        active_interval: int = CONF_ACTIVE_INTERVAL_DEFAULT,
        max_location_age: int = CONF_MAX_LOCATION_AGE_DEFAULT,
    ) -> None:
        """Initialise the coordinator."""
        self.session = session
        self.devices = devices
        self.cookie = cookie
        self.active_smarttags = active_smarttags
        self.active_others = active_others
        self.renew_cookie = renew_cookie
        self.account_flags: dict[str, Any] = {}
        self.active_interval = max(int(active_interval), 0)
        self.max_location_age = max(int(max_location_age), 0)
        self._last_request: dict[str, float] = {}
        self._last_renew = 0.0
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the locations from Samsung Find."""
        try:
            return await self._async_fetch_all()
        except ConfigEntryAuthFailed:
            if not await self._async_renew_session():
                raise
            _LOGGER.debug("Samsung Find session renewed, retrying the update")
            return await self._async_fetch_all()

    def _device(self, dev_id: str) -> dict[str, Any] | None:
        """Return the raw device dict of one device id."""
        return next(
            (item["data"] for item in self.devices if item["data"].get("dvceID") == dev_id),
            None,
        )

    async def _async_csrf(self) -> str:
        """Fetch a CSRF token, renewing the web session once if needed."""
        try:
            return await fetch_csrf(self.session, self.cookie)
        except ConfigEntryAuthFailed:
            if not await self._async_renew_session():
                raise
            return await fetch_csrf(self.session, self.cookie)

    async def async_ring(self, dev_id: str, start: bool) -> None:
        """Start or stop ringing a device."""
        device = self._device(dev_id)
        if device is None:
            return
        csrf = await self._async_csrf()
        code = await async_request_ring(self.session, self.cookie, csrf, device, start)
        if code not in (None, "00"):
            raise HomeAssistantError(f"三星拒绝了响铃请求（resultCode {code}）")

    async def async_refresh_device(self, dev_id: str) -> None:
        """Ask Samsung to locate one device right now (used by the button)."""
        device = self._device(dev_id)
        if device is None:
            return
        csrf = await self._async_csrf()

        self._last_request[dev_id] = time.monotonic()
        previous = ((self.data or {}).get(dev_id) or {}).get("used_loc") or {}
        result = await get_device_location(
            self.session, self.cookie, csrf, device, active=True
        )
        for _attempt in range(REFRESH_POLL_ATTEMPTS):
            location = result.get("used_loc") or {}
            if location and (not previous or location["gps_date"] > previous["gps_date"]):
                break
            await asyncio.sleep(REFRESH_POLL_SECONDS)
            result = await get_device_location(
                self.session, self.cookie, csrf, device, active=False
            )
        # Battery lives in the CHECK_CONNECTION operation, which is a separate
        # request; ask for it as well so one button press updates both.
        await async_request_check(self.session, self.cookie, csrf, device)
        previous_battery = result.get("battery_updated")
        for _attempt in range(REFRESH_POLL_ATTEMPTS):
            await asyncio.sleep(REFRESH_POLL_SECONDS)
            refreshed = await get_device_location(
                self.session, self.cookie, csrf, device, active=False
            )
            result = refreshed
            if refreshed.get("battery_updated") and (
                not previous_battery or refreshed["battery_updated"] > previous_battery
            ):
                break

        if self.data is None:
            self.data = {}
        self.data[dev_id] = result
        self.async_update_listeners()
        _LOGGER.debug("Manual refresh finished for %s", device.get("modelName"))

    def _may_request(self, dev_id: str) -> bool:
        """Respect the minimum time between two automatic location requests."""
        last = self._last_request.get(dev_id)
        if last is None:
            return True
        return time.monotonic() - last >= self.active_interval

    async def _async_renew_session(self) -> bool:
        """Mint a new web session (only possible with the account grant)."""
        if self.renew_cookie is None:
            return False
        now = time.monotonic()
        if now - self._last_renew < SESSION_RENEW_MIN_INTERVAL:
            return False
        self._last_renew = now
        try:
            self.cookie = await self.renew_cookie()
        except Exception as err:  # noqa: BLE001 - any failure means "ask the user"
            _LOGGER.warning("Could not renew the Samsung Find session: %s", err)
            return False
        return True

    async def _async_fetch_all(self) -> dict[str, Any]:
        """Read the locations of all devices with the current cookie."""
        try:
            # The token is cheap to refresh and doubles as a session check
            csrf = await fetch_csrf(self.session, self.cookie)
            tags: dict[str, Any] = {}
            for device in self.devices:
                dev_data = device["data"]
                is_tag = dev_data.get("deviceTypeCode") == "TAG"
                active = self.active_smarttags if is_tag else self.active_others
                requested = False
                if active and self._may_request(dev_data["dvceID"]):
                    # Battery lives in CHECK_CONNECTION, location in LOCATION:
                    # active mode asks for both, passive mode only reads.
                    self._last_request[dev_data["dvceID"]] = time.monotonic()
                    requested = True
                    await async_request_check(self.session, self.cookie, csrf, dev_data)
                tags[dev_data["dvceID"]] = await get_device_location(
                    self.session, self.cookie, csrf, dev_data, active=requested
                )
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Cannot reach Samsung Find: {err}") from err
        self._async_update_location_issue(tags)
        _LOGGER.debug("Samsung Find: fetched %s device states", len(tags))
        return tags

    @callback
    def _async_update_location_issue(self, tags: dict[str, Any]) -> None:
        """Warn when Samsung does not allow a device to report positions."""
        blocked = [
            str(entry.get("dev_name"))
            for entry in tags.values()
            if entry and entry.get("location_allowed") is False
        ]
        if blocked:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_LOCATION_NOT_ALLOWED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="location_not_allowed",
                translation_placeholders={"devices": ", ".join(sorted(set(blocked)))},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_LOCATION_NOT_ALLOWED)
