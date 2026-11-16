"""Config flow for the Samsung Find (China) integration.

Two ways to sign in:

* **三星账号（推荐）** - one interactive sign-in on Samsung's own page.  The
  password and the second factor are typed on ``account.samsung.cn``; the
  integration only stores the long lived ``userauth_token`` and uses it to mint
  new web sessions by itself.
* **Cookie** - paste a ``JSESSIONID`` copied from a logged-in browser.

The China site has no usable password API (RSA-encrypted password field,
reCAPTCHA and MFA), which is why the account method goes through Samsung's own
``ANDROIDSDK`` app channel instead.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .auth import (
    DEFAULT_DEVICE_NAME,
    AccountAuthError,
    AccountLoginExpiredError,
    AccountRedirectError,
    async_complete_login,
    async_mint_session,
    async_start_login,
)
from .const import (
    AUTH_METHOD_ACCOUNT,
    AUTH_METHOD_COOKIE,
    CONF_ACCOUNT,
    CONF_ACTIVE_INTERVAL,
    CONF_ACTIVE_INTERVAL_DEFAULT,
    CONF_ACTIVE_INTERVAL_MIN,
    CONF_ACTIVE_MODE_OTHERS,
    CONF_ACTIVE_MODE_OTHERS_DEFAULT,
    CONF_ACTIVE_MODE_SMARTTAGS,
    CONF_ACTIVE_MODE_SMARTTAGS_DEFAULT,
    CONF_AUTH_METHOD,
    CONF_CALLBACK_URL,
    CONF_COOKIE,
    CONF_DEVICE_NAME,
    CONF_SIGNIN_URL,
    CONF_JSESSIONID,
    CONF_MAX_LOCATION_AGE,
    CONF_MAX_LOCATION_AGE_DEFAULT,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATE_INTERVAL_DEFAULT,
    CONF_UPDATE_INTERVAL_MIN,
    DOMAIN,
)
from .utils import create_api_session, fetch_csrf, normalize_cookie

_LOGGER = logging.getLogger(__name__)

ENTRY_VERSION = 5
TITLE = "Samsung Find（查找我的手机）"

MODE_PASSIVE = "passive"
MODE_ACTIVE = "active"
FIELD_MODE_SMARTTAGS = "mode_smarttags"
FIELD_MODE_OTHERS = "mode_others"

MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[MODE_PASSIVE, MODE_ACTIVE],
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="mode",
    )
)


async def async_validate_cookie(hass: HomeAssistant, cookie: str) -> None:
    """Check a cookie against the live service using a throw-away session."""
    session = create_api_session(hass, auto_cleanup=False)
    try:
        await fetch_csrf(session, cookie)
    finally:
        await session.close()


def _mode_value(options: dict[str, Any], key: str, default: bool) -> str:
    """Existing boolean option -> dropdown value."""
    return MODE_ACTIVE if options.get(key, default) else MODE_PASSIVE


def _settings_schema(options: dict[str, Any]) -> dict[Any, Any]:
    """The shared option fields (mode dropdowns + numeric limits)."""
    return {
        vol.Optional(
            FIELD_MODE_OTHERS,
            default=_mode_value(
                options, CONF_ACTIVE_MODE_OTHERS, CONF_ACTIVE_MODE_OTHERS_DEFAULT
            ),
        ): MODE_SELECTOR,
        vol.Optional(
            FIELD_MODE_SMARTTAGS,
            default=_mode_value(
                options, CONF_ACTIVE_MODE_SMARTTAGS, CONF_ACTIVE_MODE_SMARTTAGS_DEFAULT
            ),
        ): MODE_SELECTOR,
        vol.Optional(
            CONF_UPDATE_INTERVAL,
            default=options.get(CONF_UPDATE_INTERVAL, CONF_UPDATE_INTERVAL_DEFAULT),
        ): vol.All(vol.Coerce(int), vol.Range(min=CONF_UPDATE_INTERVAL_MIN)),
        vol.Optional(
            CONF_ACTIVE_INTERVAL,
            default=options.get(CONF_ACTIVE_INTERVAL, CONF_ACTIVE_INTERVAL_DEFAULT),
        ): vol.All(vol.Coerce(int), vol.Range(min=CONF_ACTIVE_INTERVAL_MIN)),
        vol.Optional(
            CONF_MAX_LOCATION_AGE,
            default=options.get(CONF_MAX_LOCATION_AGE, CONF_MAX_LOCATION_AGE_DEFAULT),
        ): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }


def _options_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Dropdown/number input -> stored options."""
    return {
        CONF_ACTIVE_MODE_OTHERS: user_input.get(FIELD_MODE_OTHERS) == MODE_ACTIVE,
        CONF_ACTIVE_MODE_SMARTTAGS: user_input.get(FIELD_MODE_SMARTTAGS) == MODE_ACTIVE,
        CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
        CONF_ACTIVE_INTERVAL: int(user_input[CONF_ACTIVE_INTERVAL]),
        CONF_MAX_LOCATION_AGE: int(user_input[CONF_MAX_LOCATION_AGE]),
    }


def _cookie_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_JSESSIONID): str})


def _callback_schema(
    signin_url: str = "", device_name: str = DEFAULT_DEVICE_NAME
) -> vol.Schema:
    """Callback field plus a copyable field that carries the sign-in URL.

    The sign-in URL is ~1800 characters long; rendering it as a markdown link
    in the description is unreliable (copying it gives the dashboard URL), so
    it is also exposed as a prefilled text field the user can select and copy.
    """
    return vol.Schema(
        {
            vol.Optional(CONF_SIGNIN_URL, default=signin_url): str,
            vol.Required(CONF_CALLBACK_URL): str,
            vol.Optional(CONF_DEVICE_NAME, default=device_name): str,
        }
    )


class SamsungFindConfigFlow(ConfigFlow, domain=DOMAIN):
    """Cookie and Samsung Account setup, reauthentication, reconfiguration."""

    VERSION = ENTRY_VERSION

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._pending: dict[str, Any] | None = None
        self._signin_url: str | None = None
        self._device_name: str = DEFAULT_DEVICE_NAME
        self._reauth_entry: ConfigEntry | None = None

    # -- helpers ------------------------------------------------------------

    async def _ensure_signin_url(self) -> None:
        """Create a sign-in link once per flow (valid for 15 minutes)."""
        if self._pending is not None:
            return
        session = async_get_clientsession(self.hass)
        self._signin_url, self._pending = await async_start_login(
            session, device_name=self._device_name
        )

    async def _async_account_login(self, callback_url: str) -> tuple[dict, str]:
        """Exchange the pasted callback for a grant and a working session."""
        session = async_get_clientsession(self.hass)
        grant = await async_complete_login(session, self._pending or {}, callback_url)

        auth_session = async_create_clientsession(
            self.hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
        )
        try:
            jsessionid = await async_mint_session(auth_session, grant)
        finally:
            await auth_session.close()

        # Prove that the freshly minted session really is accepted
        await self._async_check_cookie(f"JSESSIONID={jsessionid}")
        return grant, jsessionid

    async def _async_check_cookie(self, cookie: str) -> None:
        """Validate a cookie (shared helper)."""
        await async_validate_cookie(self.hass, cookie)

    async def _async_create_or_update(self, data: dict[str, Any]) -> Any:
        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(self._reauth_entry, data=data)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=TITLE, data=data)

    # -- flows --------------------------------------------------------------

    async def async_migrate_entry(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Add the auth method to entries created before 0.6."""
        if config_entry.version < ENTRY_VERSION:
            data = dict(config_entry.data)
            data.setdefault(CONF_AUTH_METHOD, AUTH_METHOD_COOKIE)
            hass.config_entries.async_update_entry(
                config_entry, data=data, version=ENTRY_VERSION
            )
        return True

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Let the user pick a sign-in method."""
        return self.async_show_menu(
            step_id="user", menu_options=["account", "cookie"]
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Samsung Account sign-in through the ms-app callback."""
        errors: dict[str, str] = {}
        try:
            await self._ensure_signin_url()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not create a Samsung sign-in link")
            errors["base"] = "cannot_connect"

        if user_input is not None and not errors:
            name = (user_input.get(CONF_DEVICE_NAME) or "").strip()
            if name and name != self._device_name and self._pending is None:
                self._device_name = name[:64]
            callback = (user_input.get(CONF_CALLBACK_URL) or "").strip()
            if CONF_CALLBACK_URL in user_input:
                try:
                    grant, jsessionid = await self._async_account_login(callback)
                except AccountLoginExpiredError:
                    errors["base"] = "login_expired"
                    self._pending = None
                    self._signin_url = None
                except AccountRedirectError:
                    errors["base"] = "invalid_callback"
                except AccountAuthError:
                    _LOGGER.exception("Samsung Account sign-in rejected")
                    errors["base"] = "auth_failed"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Samsung Account sign-in failed")
                    errors["base"] = "cannot_connect"
                else:
                    return await self._async_create_or_update(
                        {
                            CONF_AUTH_METHOD: AUTH_METHOD_ACCOUNT,
                            CONF_ACCOUNT: grant,
                            CONF_JSESSIONID: jsessionid,
                        }
                    )

        # A failed attempt with an expired link needs a fresh one
        if self._pending is None:
            try:
                await self._ensure_signin_url()
            except Exception:  # noqa: BLE001
                pass

        return self.async_show_form(
            step_id="account",
            data_schema=_callback_schema(self._signin_url or "", self._device_name),
            errors=errors,
            description_placeholders={"signin_url": self._signin_url or ""},
        )

    async def async_step_cookie(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Accept a Find session cookie."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cookie = normalize_cookie(user_input[CONF_JSESSIONID])
            try:
                if not cookie:
                    raise ValueError("empty cookie")
                await self._async_check_cookie(cookie)
            except Exception:  # noqa: BLE001 - every failure means "bad cookie"
                _LOGGER.debug("Cookie validation failed", exc_info=True)
                errors["base"] = "invalid_cookie"
            else:
                return await self._async_create_or_update(
                    {CONF_AUTH_METHOD: AUTH_METHOD_COOKIE, CONF_JSESSIONID: cookie}
                )
        return self.async_show_form(
            step_id="cookie", data_schema=_cookie_schema(), errors=errors
        )

    # -- reauth / reconfigure ----------------------------------------------

    def _remember_entry(self) -> None:
        entry_id = self.context.get("entry_id")
        if entry_id:
            self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> Any:
        """Start reauthentication."""
        self._remember_entry()
        return self.async_show_menu(
            step_id="reauth", menu_options=["account", "cookie"]
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Replace the stored sign-in without removing the entry."""
        self._remember_entry()
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["account", "cookie"]
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SamsungFindOptionsFlow()


class SamsungFindOptionsFlow(OptionsFlow):
    """Polling options, written the same way the upstream integration does it."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """Manage the options."""
        entry = self.config_entry
        cookie_mode = (
            entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_COOKIE) == AUTH_METHOD_COOKIE
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            new_cookie = normalize_cookie(str(user_input.get(CONF_COOKIE) or "").strip())
            current = entry.data.get(CONF_JSESSIONID, "")
            if new_cookie and new_cookie != current:
                try:
                    await async_validate_cookie(self.hass, new_cookie)
                except Exception:  # noqa: BLE001 - any failure means "bad cookie"
                    _LOGGER.debug("Cookie validation in options failed", exc_info=True)
                    errors["base"] = "invalid_cookie"
                else:
                    # Data and options in one update, so the entry reloads once
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_JSESSIONID: new_cookie},
                    )
            if not errors:
                return self.async_create_entry(data=_options_from_input(user_input))

        schema: dict[Any, Any] = {}
        if cookie_mode:
            schema[vol.Optional(CONF_COOKIE)] = selector.TextSelector(
                selector.TextSelectorConfig(
                    multiline=True, type=selector.TextSelectorType.PASSWORD
                )
            )
        schema.update(_settings_schema(entry.options))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )
