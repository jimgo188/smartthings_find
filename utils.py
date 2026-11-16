"""Helpers for the private Samsung Find (China) web API.

Everything below was verified against the live service
https://findmymobile.samsung.cn (November 2026):

  GET  /init.do                    -> 200 + ``_csrf`` response header
                                      (403 + body "logout" when the cookie died)
  POST /device/getDeviceList.do    -> {"deviceList": [...], "childrenDeviceList": [...]}
                                      (401 + body "Logout" when the cookie died)
  POST /device/setLastSelect.do    -> {"operation": [...], "resultCode": "00", ...}
                                      needs body {"dvceId": ..., "removeDevice": []}
  POST /dm/addOperation.do         -> {"oprnType": "LOCATION", "reqId": "...",
                                       "resultCode": "00"} when accepted

Requesting a fresh position is ``operation: "LOCATION"``.  The device answers
through Samsung's push channel, so the new position shows up in a *later*
``setLastSelect`` call (observed: about 15 s).  ``CHECK_CONNECTION_WITH_LOCATION``
is NOT accepted by this account/device - it always answers ``resultCode: "02"``
without a ``reqId``.

Authentication is a single ``JSESSIONID`` cookie copied from a browser that is
already logged in to that site.  Home Assistant never sees the password.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import BATTERY_LEVELS, DOMAIN, SERVICE_BASE

_LOGGER = logging.getLogger(__name__)

URL_INIT = f"{SERVICE_BASE}/init.do"
URL_DEVICE_LIST = f"{SERVICE_BASE}/device/getDeviceList.do"
URL_REQUEST_LOC_UPDATE = f"{SERVICE_BASE}/dm/addOperation.do"
URL_SET_LAST_DEVICE = f"{SERVICE_BASE}/device/setLastSelect.do"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Keys of hass.data[DOMAIN][entry_id]
DATA_COOKIE = "cookie"
DATA_CSRF = "csrf"
DATA_SESSION = "session"
DATA_COORDINATOR = "coordinator"
DATA_DEVICES = "devices"

# Operation types that carry coordinates
LOCATION_OPS = ("LOCATION", "LASTLOC", "OFFLINE_LOC")
# Operation types that mean "the device already gave up" (2900 + 1452)
FAILED_OP_STATUS = "2900"

# After an accepted LOCATION request the device answers asynchronously
LOCATION_WAIT_ATTEMPTS = 3
LOCATION_WAIT_SECONDS = 5


def create_api_session(hass: HomeAssistant, *, auto_cleanup: bool = True):
    """Create a session for the Find API calls.

    The jar is a ``DummyCookieJar`` on purpose: aiohttp replaces an explicit
    ``Cookie`` header with a same-named cookie from the jar, and Samsung sends
    an anonymous ``JSESSIONID`` with every 401/403 answer.  With a real jar one
    rejected request would therefore poison all following ones.
    """
    return async_create_clientsession(
        hass, cookie_jar=aiohttp.DummyCookieJar(), auto_cleanup=auto_cleanup
    )


def normalize_cookie(raw_cookie: str) -> str:
    """Turn whatever the user pasted into a usable Cookie header value."""
    value = (raw_cookie or "").strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    value = value.strip().strip('"').strip("'").strip()
    if value and "=" not in value:
        # Only the bare JSESSIONID value was pasted
        value = f"JSESSIONID={value}"
    return value


def build_headers(cookie: str, *, json_body: bool = True) -> dict[str, str]:
    """Build the headers the web front end sends."""
    headers = {
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"{SERVICE_BASE}/",
        "User-Agent": USER_AGENT,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


#: Consent switches Samsung returns from /init.do; "Y" means accepted
AGREEMENT_KEYS = {
    "agmtPrivacyFg": "个人信息收集与使用",
    "agmtLocationFg": "位置信息使用",
    "agmtSim": "SIM 卡历史记录",
    "agmtCallLog": "通话记录",
}


async def async_get_account_flags(session: Any, cookie: str) -> dict[str, Any]:
    """Read the account consent switches (``agmt*``) from /init.do."""
    try:
        async with session.get(
            URL_INIT, headers=build_headers(cookie, json_body=False)
        ) as response:
            if response.status != 200:
                return {}
            payload = await _read_json(response)
    except Exception:  # noqa: BLE001 - purely informational
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: payload.get(key)
        for key in AGREEMENT_KEYS
        if payload.get(key) is not None
    }


def _is_logged_out(status: int, body: str) -> bool:
    """The service answers 401/403 with the word 'logout' when the cookie died."""
    if status in (401, 403):
        return True
    return (body or "").strip().lower() in ("logout", "log out")


async def _read_json(response: Any) -> Any:
    """Best effort JSON read; returns None instead of raising."""
    try:
        return await response.json(content_type=None)
    except Exception:  # noqa: BLE001 - any decode problem counts as "no data"
        try:
            return json.loads(await response.text())
        except Exception:  # noqa: BLE001
            return None


async def fetch_csrf(session: Any, cookie: str) -> str:
    """Fetch the ``_csrf`` token that every following request has to carry.

    Raises ConfigEntryAuthFailed when the cookie is not (or no longer) valid.
    """
    async with session.get(
        URL_INIT, headers=build_headers(cookie, json_body=False)
    ) as response:
        body = await response.text()
        token = response.headers.get("_csrf") or response.headers.get("_CSRF")
        if response.status == 200 and token:
            _LOGGER.debug("Fetched new _csrf token")
            return token
        if _is_logged_out(response.status, body):
            raise ConfigEntryAuthFailed(
                f"Samsung Find session is not valid (HTTP {response.status})"
            )
        raise ConfigEntryAuthFailed(
            f"No _csrf token in /init.do response (HTTP {response.status})"
        )


async def get_devices(
    hass: HomeAssistant, session: Any, cookie: str, csrf: str
) -> list[dict[str, Any]]:
    """Return the devices of the account, shaped for the coordinator/entities."""
    async with session.post(
        URL_DEVICE_LIST,
        params={"_csrf": csrf},
        json={},
        headers=build_headers(cookie),
    ) as response:
        body = await response.text()
        if _is_logged_out(response.status, body):
            raise ConfigEntryAuthFailed(
                f"Request to get the device list failed: HTTP {response.status}"
            )
        if response.status != 200:
            _LOGGER.error("Failed to retrieve devices [%s]", response.status)
            raise ConfigEntryAuthFailed(
                f"Request to get the device list failed: HTTP {response.status}"
            )
        try:
            payload = json.loads(body)
        except ValueError:
            _LOGGER.error("Device list is not JSON: %s", body[:200])
            raise ConfigEntryAuthFailed("Device list is not JSON")

    raw_devices: list[dict[str, Any]] = list(payload.get("deviceList") or [])
    # Family / child accounts are nested one level deeper (seen in the API)
    for child in payload.get("childrenDeviceList") or []:
        raw_devices.extend(child.get("deviceList") or [])

    devices: list[dict[str, Any]] = []
    for device in raw_devices:
        dvce_id = device.get("dvceID")
        if not dvce_id:
            continue
        # Double unescaping required, e.g. "Jim&amp;#39;s S24" -> "Jim's S24"
        model_name = html.unescape(html.unescape(device.get("modelName") or dvce_id))
        device["modelName"] = model_name
        identifier = (DOMAIN, dvce_id)
        ha_dev = device_registry.async_get(hass).async_get_device({identifier})
        if ha_dev and ha_dev.disabled:
            _LOGGER.debug("Ignoring disabled device: '%s'", model_name)
            continue
        ha_dev_info = DeviceInfo(
            identifiers={identifier},
            manufacturer="Samsung",
            name=model_name,
            model=device.get("modelID"),
            configuration_url=SERVICE_BASE,
        )
        devices.append({"data": device, "ha_dev_info": ha_dev_info})
        _LOGGER.debug("Adding device: %s (%s)", model_name, dvce_id)
    return devices


def _operation_date(op: dict[str, Any], loc: dict[str, Any] | None = None) -> datetime | None:
    """Return the timestamp of an operation (gpsUtcDt, else oprnDoneDate)."""
    source = loc if loc is not None else op
    raw = (op.get("extra") or {}).get("gpsUtcDt") or source.get("gpsUtcDt")
    if not raw:
        raw = op.get("oprnDoneDate") or op.get("oprnCrtDate")
    if not raw:
        return None
    try:
        return parse_stf_date(raw)
    except ValueError:
        return None


def _pick_location(
    ops: list[dict[str, Any]], dev_name: str
) -> tuple[dict | None, dict | None]:
    """Pick the newest usable location out of the operation list."""
    used_op: dict[str, Any] | None = None
    used_loc: dict[str, Any] | None = None

    for op in ops:
        if op.get("oprnType") not in LOCATION_OPS:
            continue
        if op.get("oprnStsCd") == FAILED_OP_STATUS:
            _LOGGER.debug("[%s] skipping failed %s operation", dev_name, op.get("oprnType"))
            continue

        if "latitude" in op:
            gps_date = _operation_date(op)
            if gps_date is None:
                continue
            if used_loc and used_loc["gps_date"] and used_loc["gps_date"] >= gps_date:
                continue
            used_loc = {
                "latitude": _to_float(op.get("latitude")),
                "longitude": _to_float(op.get("longitude")),
                "gps_accuracy": calc_gps_accuracy(
                    op.get("horizontalUncertainty"), op.get("verticalUncertainty")
                ),
                "gps_date": gps_date,
                "oprn_type": op.get("oprnType"),
            }
            used_op = op
        elif "encLocation" in op:
            loc = op.get("encLocation") or {}
            if loc.get("encrypted"):
                _LOGGER.debug(
                    "[%s] ignoring encrypted location (%s)", dev_name, op.get("oprnType")
                )
                continue
            gps_date = _operation_date(op, loc)
            if gps_date is None:
                continue
            if used_loc and used_loc["gps_date"] and used_loc["gps_date"] >= gps_date:
                continue
            used_loc = {
                "latitude": _to_float(loc.get("latitude")),
                "longitude": _to_float(loc.get("longitude")),
                "gps_accuracy": calc_gps_accuracy(
                    loc.get("horizontalUncertainty"), loc.get("verticalUncertainty")
                ),
                "gps_date": gps_date,
                "oprn_type": op.get("oprnType"),
            }
            used_op = op

    if used_loc and (used_loc["latitude"] is None or used_loc["longitude"] is None):
        return None, None
    return used_op, used_loc


def _battery_timestamp(ops: list[dict[str, Any]] | None) -> datetime | None:
    """Return when the CHECK_CONNECTION operation (the battery source) ran."""
    for op in ops or []:
        if op.get("oprnType") != "CHECK_CONNECTION":
            continue
        raw = op.get("oprnDoneDate") or op.get("oprnCrtDate")
        if not raw:
            return None
        try:
            return parse_stf_date(raw)
        except ValueError:
            return None
    return None


def get_connection_state(ops: list[dict[str, Any]] | None) -> bool | None:
    """Return whether the device is currently reachable (CHECK_CONNECTION op)."""
    for op in ops or []:
        if op.get("oprnType") == "CHECK_CONNECTION":
            extra = op.get("extra") or {}
            if "isConnected" in extra:
                return bool(extra["isConnected"])
    return None


async def _read_operations(
    session: Any, cookie: str, csrf: str, dev_id: str
) -> tuple[bool, dict[str, Any], str | None]:
    """Read the stored operations (and feature menu) of one device."""
    async with session.post(
        URL_SET_LAST_DEVICE,
        params={"_csrf": csrf},
        json={"dvceId": dev_id, "removeDevice": []},
        headers=build_headers(cookie),
    ) as response:
        body_text = await response.text()
        if _is_logged_out(response.status, body_text):
            raise ConfigEntryAuthFailed(
                f"Session not valid anymore (HTTP {response.status})"
            )
        if response.status != 200:
            return False, {}, f"HTTP {response.status}"
        try:
            data = json.loads(body_text)
        except ValueError:
            _LOGGER.warning("setLastSelect is not JSON: %s", body_text[:200])
            return False, {}, "invalid JSON"
    if not isinstance(data, dict):
        return False, {}, "unexpected response"
    return True, data, None


def parse_menu(menu: Any) -> dict[str, bool]:
    """Flatten Samsung's ``menu`` list into ``feature -> supported``.

    The list contains both plain switches (``{"ring": "Y"}``) and nested ones
    (``{"lock": [{"screenLock": "Y"}, ...]}``).
    """
    features: dict[str, bool] = {}
    if not isinstance(menu, list):
        return features
    for item in menu:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if isinstance(value, str):
                features[key] = value.upper() == "Y"
            elif isinstance(value, list):
                for sub in value:
                    if not isinstance(sub, dict):
                        continue
                    for sub_key, sub_value in sub.items():
                        if isinstance(sub_value, str):
                            features[sub_key] = sub_value.upper() == "Y"
    return features


def get_connection_info(ops: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Reachability, lock state and timestamp of the CHECK_CONNECTION answer."""
    for op in ops or []:
        if op.get("oprnType") != "CHECK_CONNECTION":
            continue
        extra = op.get("extra") or {}
        lock = extra.get("lockStatus") or {}
        raw = op.get("oprnDoneDate") or op.get("oprnCrtDate")
        try:
            checked_at = parse_stf_date(raw) if raw else None
        except ValueError:
            checked_at = None
        return {
            "connected": extra.get("isConnected"),
            # normalLock: true = screen is locked, fmmLock: remote lock
            "lock_locked": lock.get("normalLock"),
            "lock_remote": lock.get("fmmLock"),
            "checked_at": checked_at,
        }
    return {}


def location_allowed(menu: Any) -> bool | None:
    """Read the per-device "location" switch out of the ``menu`` list.

    Samsung only delivers locations after the user accepted the location /
    personal-data agreements for that device on the website.  Until then
    location requests are accepted (``resultCode "00"``) but never fulfilled,
    so the last known position stays hours old.
    """
    if not isinstance(menu, list):
        return None
    for item in menu:
        if isinstance(item, dict) and "location" in item:
            return str(item["location"]).upper() == "Y"
    return None


async def async_request_check(
    session: Any, cookie: str, csrf: str, dev_data: dict[str, Any]
) -> str | None:
    """Ask the device for a status report (this is what carries the battery)."""
    return await _request_operation(session, cookie, csrf, dev_data, "CHECK_CONNECTION")


async def _request_location(
    session: Any, cookie: str, csrf: str, dev_data: dict[str, Any]
) -> str | None:
    """Ask the device for a fresh position; returns the API resultCode."""
    operation: Any = "LOCATION"
    if dev_data.get("deviceType") == "BUDS":
        # the web front end asks earbuds for LOCATION + LASTLOC
        operation = ["LOCATION", "LASTLOC"]
    return await _request_operation(session, cookie, csrf, dev_data, operation)


RING_MESSAGE = "Home Assistant 正在让你的手机响铃"


async def async_request_ring(
    session: Any, cookie: str, csrf: str, dev_data: dict[str, Any], start: bool
) -> str | None:
    """Start or stop ringing a device."""
    extra: dict[str, Any] = {"status": "start" if start else "stop"}
    if start:
        extra["lockMessage"] = RING_MESSAGE
    return await _request_operation(session, cookie, csrf, dev_data, "RING", **extra)


async def _request_operation(
    session: Any,
    cookie: str,
    csrf: str,
    dev_data: dict[str, Any],
    operation: Any,
    **extra: Any,
) -> str | None:
    """Post one addOperation request and return its resultCode."""
    payload = {
        "dvceId": dev_data["dvceID"],
        "operation": operation,
        "usrId": dev_data.get("usrId"),
        **extra,
    }
    async with session.post(
        URL_REQUEST_LOC_UPDATE,
        params={"_csrf": csrf},
        json=payload,
        headers=build_headers(cookie),
    ) as response:
        body_text = await response.text()
        if _is_logged_out(response.status, body_text):
            raise ConfigEntryAuthFailed(
                f"Session expired while requesting a location (HTTP {response.status})"
            )
        try:
            body = json.loads(body_text) if body_text else {}
        except ValueError:
            body = {}
    if response.status != 200:
        _LOGGER.debug("[%s] location request: HTTP %s", dev_data.get("modelName"), response.status)
        return None
    return body.get("resultCode")


async def get_device_location(
    session: Any,
    cookie: str,
    csrf: str,
    dev_data: dict[str, Any],
    *,
    active: bool,
) -> dict[str, Any]:
    """Read (and optionally refresh) the last known location of one device.

    Never returns None: the dict always has the same keys so the entities can
    read it without null checks.
    """
    dev_id = dev_data["dvceID"]
    dev_name = dev_data.get("modelName") or dev_id

    result: dict[str, Any] = {
        "dev_name": dev_name,
        "dev_id": dev_id,
        "update_success": False,
        "location_found": False,
        "used_op": None,
        "used_loc": None,
        "ops": [],
        "requested": active,
        "request_code": None,
        "connected": None,
        "location_allowed": None,
        "battery_updated": None,
        "connection": {},
        "menu": {},
        "error": None,
        "polled_at": datetime.now(timezone.utc),
    }

    try:
        ok, data, error = await _read_operations(session, cookie, csrf, dev_id)
        if not ok:
            result["error"] = error
            _LOGGER.warning("[%s] could not read the device state (%s)", dev_name, error)
            return result

        ops = list(data.get("operation") or [])
        result["ops"] = ops
        result["location_allowed"] = location_allowed(data.get("menu"))
        result["battery_updated"] = _battery_timestamp(ops)
        result["menu"] = parse_menu(data.get("menu"))
        result["connection"] = get_connection_info(ops)
        if result["location_allowed"] is False:
            _LOGGER.warning(
                "[%s] 三星没有允许这台设备上报位置：请在 findmymobile.samsung.cn 上"
                "同意该设备的位置/个人信息使用条款",
                dev_name,
            )
        result["update_success"] = True
        used_op, used_loc = _pick_location(ops, dev_name)

        if active:
            result["request_code"] = await _request_location(
                session, cookie, csrf, dev_data
            )
            if result["request_code"] == "00":
                # The device answers through Samsung's push channel, so the new
                # position only shows up in a later setLastSelect call.
                for _attempt in range(LOCATION_WAIT_ATTEMPTS):
                    await asyncio.sleep(LOCATION_WAIT_SECONDS)
                    ok, new_data, error = await _read_operations(
                        session, cookie, csrf, dev_id
                    )
                    if not ok:
                        break
                    new_ops = list(new_data.get("operation") or [])
                    used_op, used_loc = _pick_location(new_ops, dev_name)
                    result["ops"] = new_ops
                    result["location_allowed"] = location_allowed(new_data.get("menu"))
                    result["battery_updated"] = _battery_timestamp(new_ops)
                    result["menu"] = parse_menu(new_data.get("menu"))
                    result["connection"] = get_connection_info(new_ops)
                    newer = used_loc and (
                        not result["used_loc"]
                        or used_loc["gps_date"] > result["used_loc"]["gps_date"]
                    )
                    if newer:
                        break
            else:
                _LOGGER.debug(
                    "[%s] location request not accepted (resultCode=%s)",
                    dev_name,
                    result["request_code"],
                )

        result["used_op"] = used_op
        result["used_loc"] = used_loc
        result["location_found"] = used_loc is not None
        result["connected"] = get_connection_state(result["ops"])
        if used_loc is None:
            _LOGGER.debug("[%s] no usable location in the response", dev_name)
        return result

    except ConfigEntryAuthFailed:
        raise
    except Exception as err:  # noqa: BLE001 - one bad device must not kill the update
        result["error"] = str(err)
        _LOGGER.warning("[%s] could not read the location: %s", dev_name, err)
        return result


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def calc_gps_accuracy(hu: Any, vu: Any) -> float | None:
    """Combine the horizontal and vertical uncertainty into one accuracy."""
    try:
        horizontal = float(hu)
        vertical = float(vu) if vu not in (None, "") else 0.0
        return round((horizontal**2 + vertical**2) ** 0.5, 1)
    except (TypeError, ValueError):
        return None


def get_sub_location(
    ops: list[dict[str, Any]] | None, sub_device_name: str
) -> tuple[dict, dict]:
    """Extract the location of a sub device (e.g. one earbud)."""
    if not ops or not sub_device_name:
        return {}, {}
    for op in ops:
        loc = (op.get("encLocation") or {}).get(sub_device_name)
        if not loc or "latitude" not in loc:
            continue
        return op, {
            "latitude": _to_float(loc.get("latitude")),
            "longitude": _to_float(loc.get("longitude")),
            "gps_accuracy": calc_gps_accuracy(
                loc.get("horizontalUncertainty"), loc.get("verticalUncertainty")
            ),
            "gps_date": parse_stf_date(loc["gpsUtcDt"]) if loc.get("gpsUtcDt") else None,
        }
    return {}, {}


def parse_stf_date(datestr: str) -> datetime:
    """Parse the "YYYYMMDDHHMMSS" timestamps Samsung Find uses (UTC)."""
    return datetime.strptime(datestr, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def get_battery_level(dev_name: str, ops: list[dict[str, Any]] | None) -> int | None:
    """Try to read the battery level out of the operation list.

    The API sends e.g. ``"battery": "64"`` inside CHECK_CONNECTION.
    """
    for op in ops or []:
        if op.get("oprnType") != "CHECK_CONNECTION":
            continue
        batt_raw = op.get("battery") or (op.get("extra") or {}).get("battery")
        if batt_raw in (None, ""):
            continue
        if isinstance(batt_raw, int):
            return batt_raw
        batt = BATTERY_LEVELS.get(str(batt_raw))
        if batt is not None:
            return batt
        try:
            return int(batt_raw)
        except (TypeError, ValueError):
            _LOGGER.debug("[%s] unexpected battery value: %s", dev_name, batt_raw)
    return None
