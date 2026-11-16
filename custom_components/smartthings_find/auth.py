"""Samsung Account (China) authorization for Samsung Find.

The China site has no "password API": its login page is a JavaScript app that
encrypts the password with a rotating RSA key, and it is guarded by reCAPTCHA
and MFA.  Samsung's own apps therefore use a different channel, the
``ANDROIDSDK`` account protocol, which is what this module implements:

  1. ``GET  {entry_point}``                 -> chkDoNum, pkiPublicKey, signInURI
  2. user opens ``{signInURI}?svcParam=...`` -> Samsung login page (the password
                                                and the second factor are typed
                                                *there*, never here)
  3. Samsung redirects to ``ms-app://...``   -> encrypted state/code/retValue
                                                (a browser cannot open that
                                                scheme, so it is pasted back)
  4. ``POST {auth_server}/auth/oauth2/authenticate`` -> userauth_token
  5. ``GET  {auth_server}/auth/oauth2/v2/authorize``  -> short lived code
  6. ``GET  {find}/getState.do`` then ``{find}/login.do`` -> new JSESSIONID

Only the long lived ``userauth_token`` (step 4) is stored in the config entry;
it can be used to mint a new ``JSESSIONID`` whenever Samsung ends the old one,
and it stops working only when Samsung revokes it.

Protocol reference: charlesbel/samsung-re-find and fapgomes/ha-smartthings-find.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from homeassistant.exceptions import HomeAssistantError

from .const import SERVICE_BASE

_LOGGER = logging.getLogger(__name__)

# Samsung China account channel
ENTRY_POINT_URL = "https://account.samsung.cn/accounts/ANDROIDSDK/getEntryPoint"
# Redirect scheme of Samsung's own desktop app; the browser cannot open it,
# which is exactly why the address can be copied out of DevTools.
REDIRECT_URI = (
    "ms-app://s-1-15-2-4027708247-2189610-1983755848-2937435718-"
    "1578786913-2158692839-1974417358"
)
AUTH_CLIENT_ID = "yfrtglt53o"        # Samsung Account app client
WEB_FIND_CLIENT_ID = "ntly6zvfpn"    # SmartThings Find web client
WEB_FIND_SCOPE = "iot.client"
DEFAULT_COUNTRY = "cn"
DEFAULT_LOCALE = "zh-CN"
#: Name shown in the Samsung Account "signed-in devices" list
DEFAULT_DEVICE_NAME = "Home Assistant"

#: A sign-in link must be completed within this time.
PENDING_MAX_AGE = 900

_TRUSTED_ACCOUNT_DOMAINS = (
    "samsung.com",
    "samsung.cn",
    "samsungosp.com",
    "samsungosp.com.cn",
)


class AccountAuthError(HomeAssistantError):
    """Samsung rejected the sign-in or the stored account grant."""


class AccountRedirectError(AccountAuthError):
    """The pasted address is not a usable sign-in callback."""


class AccountLoginExpiredError(AccountAuthError):
    """The sign-in link is too old; a new one is needed."""


def _urlsafe_random(byte_count: int) -> str:
    """Return a URL-safe random string."""
    return base64.urlsafe_b64encode(os.urandom(byte_count)).rstrip(b"=").decode()


def _code_challenge(verifier: str) -> str:
    """PKCE S256 challenge."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def encrypt_svc_param(payload: dict[str, Any], chk_do_num: int, public_key_b64: str) -> str:
    """Encrypt the sign-in parameters the way ``signInGate`` expects them."""
    chk_text = str(chk_do_num)
    hashed = base64.b64encode(hashlib.sha256(chk_text.encode()).digest())
    derived = hashlib.pbkdf2_hmac("sha256", hashed, os.urandom(16), chk_do_num, dklen=16)
    public_key = serialization.load_der_public_key(base64.b64decode(public_key_b64))
    encrypted_key = public_key.encrypt(  # type: ignore[union-attr]
        base64.b64encode(derived), asym_padding.PKCS1v15()
    )
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(derived), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    envelope = {
        "chkDoNum": chk_text,
        "svcEncParam": base64.b64encode(encrypted).decode(),
        "svcEncKY": base64.b64encode(encrypted_key).decode(),
        "svcEncIV": iv.hex(),
    }
    return quote(base64.b64encode(json.dumps(envelope).encode()).decode(), safe="")


def decrypt_callback_value(value: str, key: str) -> str:
    """Decrypt one field of the sign-in callback (AES-ECB, as Samsung sends it)."""
    key_bytes = key.encode()[:16].ljust(16, b"\0")
    decryptor = Cipher(algorithms.AES(key_bytes), modes.ECB()).decryptor()  # noqa: S305
    padded = decryptor.update(bytes.fromhex(value)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


def encrypt_callback_value(value: str, key: str) -> str:
    """Inverse of :func:`decrypt_callback_value` (used by the tests)."""
    key_bytes = key.encode()[:16].ljust(16, b"\0")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key_bytes), modes.ECB()).encryptor()  # noqa: S305
    return (encryptor.update(padded) + encryptor.finalize()).hex()


def trusted_auth_server(value: str) -> str:
    """Normalise the auth server host Samsung names; refuse anything foreign."""
    text = (value or "").strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    trusted = any(host == d or host.endswith(f".{d}") for d in _TRUSTED_ACCOUNT_DOMAINS)
    if (
        parts.scheme != "https"
        or not trusted
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise AccountAuthError(f"Untrusted Samsung authentication server: {host or '?'}")
    return f"https://{host}"


def parse_callback(redirect_uri: str, pending: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(auth_server_url, code, login_id)`` from the pasted callback."""
    text = (redirect_uri or "").strip().strip('"').strip("'")
    expected = urlsplit(REDIRECT_URI)
    try:
        actual = urlsplit(text)
    except ValueError as err:
        raise AccountRedirectError("不是三星登录回调地址") from err
    if (actual.scheme, actual.netloc, actual.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise AccountRedirectError("不是三星登录回调地址（应以 ms-app:// 开头）")

    params = parse_qs(actual.query)
    if actual.fragment:
        params.update(parse_qs(actual.fragment))

    def one(name: str) -> str:
        return params.get(name, [""])[0]

    try:
        response_key = decrypt_callback_value(one("state"), pending["state"])
        auth_server = decrypt_callback_value(one("auth_server_url"), response_key)
        code = decrypt_callback_value(one("code"), response_key)
        login_id = decrypt_callback_value(one("retValue"), response_key)
    except (ValueError, KeyError) as err:
        raise AccountRedirectError(
            "这段回调和当前的登录链接对不上（可能复制自另一次登录）"
        ) from err
    if not code:
        raise AccountRedirectError("回调里没有授权码 code")
    return trusted_auth_server(auth_server), code, login_id


def _json_body(status: int, body: str, step: str) -> dict[str, Any]:
    """Turn a response into JSON, mapping statuses onto our exceptions."""
    if status == 429 or status >= 500:
        raise AccountAuthError(f"{step}: HTTP {status}")
    if status in (400, 401, 403):
        raise AccountAuthError(f"{step}: HTTP {status}")
    if status != 200:
        raise AccountAuthError(f"{step}: HTTP {status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as err:
        raise AccountAuthError(f"{step}: 返回内容不是 JSON") from err
    if not isinstance(data, dict):
        raise AccountAuthError(f"{step}: 返回内容格式异常")
    return data


async def async_start_login(
    session: Any,
    country: str = DEFAULT_COUNTRY,
    locale: str = DEFAULT_LOCALE,
    device_id: str | None = None,
    device_name: str = DEFAULT_DEVICE_NAME,
) -> tuple[str, dict[str, Any]]:
    """Create a sign-in link and the secrets needed to finish the login."""
    async with session.get(ENTRY_POINT_URL) as response:
        status = response.status
        body = await response.text()
    entry = _json_body(status, body, "getEntryPoint")
    try:
        chk_do_num = int(entry["chkDoNum"])
        public_key = str(entry["pkiPublicKey"])
        sign_in = str(entry["signInURI"])
    except (KeyError, TypeError, ValueError) as err:
        raise AccountAuthError("getEntryPoint: 返回内容异常") from err

    pending = {
        "state": _urlsafe_random(15)[:20],
        "code_verifier": _urlsafe_random(32)[:43],
        "device_id": device_id or secrets.token_hex(16),
        "created_at": time.time(),
    }
    payload = {
        "clientId": AUTH_CLIENT_ID,
        "code_challenge": _code_challenge(pending["code_verifier"]),
        "code_challenge_method": "S256",
        "competitorDeviceYNFlag": "Y",
        "countryCode": country.lower(),
        # Shown in the Samsung Account device list; keep it recognisable so
        # the authorization can be revoked on purpose later.
        "deviceInfo": "Google|com.android.chrome",
        "deviceModelID": device_name,
        "deviceName": device_name,
        "deviceOSVersion": "35",
        "devicePhysicalAddressText": f"ANID:{pending['device_id']}",
        "deviceType": "APP",
        "deviceUniqueID": pending["device_id"],
        "redirect_uri": REDIRECT_URI,
        "replaceableClientConnectYN": "N",
        "replaceableClientId": "",
        "replaceableDevicePhysicalAddressText": "",
        "responseEncryptionType": "1",
        "responseEncryptionYNFlag": "Y",
        "scope": "",
        "state": pending["state"],
        "svcIptLgnID": "",
        "iosYNFlag": "Y",
    }
    svc_param = encrypt_svc_param(payload, chk_do_num, public_key)
    return f"{sign_in}?locale={quote(locale)}&svcParam={svc_param}&mode=C", pending


async def async_complete_login(
    session: Any, pending: dict[str, Any], redirect_uri: str
) -> dict[str, Any]:
    """Exchange the pasted callback for a long lived account grant."""
    age = time.time() - float(pending.get("created_at", 0))
    if age < -60 or age > PENDING_MAX_AGE:
        raise AccountLoginExpiredError("登录链接已过期，请重新生成")
    auth_server, code, login_id = parse_callback(redirect_uri, pending)

    step = "Samsung Account 登录"
    async with session.post(
        f"{auth_server}/auth/oauth2/authenticate",
        data={
            "grant_type": "authorization_code",
            "serviceType": "M",
            "client_id": AUTH_CLIENT_ID,
            "code": code,
            "code_verifier": pending["code_verifier"],
            "username": login_id,
            "physical_address_text": pending["device_id"],
        },
        allow_redirects=False,
    ) as response:
        status = response.status
        body = await response.text()
    data = _json_body(status, body, step)
    token = data.get("userauth_token") or data.get("userAuthToken")
    user_id = data.get("userId") or data.get("user_id")
    if not token or not user_id:
        raise AccountAuthError(f"{step}: 响应里没有授权令牌")
    _LOGGER.debug("Samsung Account authorization obtained")
    return {
        "auth_server_url": auth_server,
        "userauth_token": str(token),
        "user_id": str(user_id),
        "login_id": login_id,
        "device_id": pending["device_id"],
    }


async def async_mint_session(session: Any, grant: dict[str, Any]) -> str:
    """Use the account grant to obtain a fresh JSESSIONID.

    ``session`` must have its own cookie jar (``CookieJar(unsafe=True)``),
    because the login is completed through cookies.
    """
    auth_server = trusted_auth_server(grant["auth_server_url"])
    params = {
        "response_type": "code",
        "serviceType": "M",
        "client_id": WEB_FIND_CLIENT_ID,
        "childAccountSupported": "Y",
        "userauth_token": grant["userauth_token"],
        "physical_address_text": grant["device_id"],
        "scope": WEB_FIND_SCOPE,
        "login_id": grant.get("login_id", ""),
    }
    step = "Samsung Find 授权"
    async with session.get(
        f"{auth_server}/auth/oauth2/v2/authorize", params=params
    ) as response:
        status = response.status
        body = await response.text()
    data = _json_body(status, body, step)
    if not data.get("code") and data.get("privacyAccepted") == "N":
        params.pop("login_id", None)
        async with session.get(
            f"{auth_server}/auth/oauth2/v2/authorize", params=params
        ) as response:
            status = response.status
            body = await response.text()
        data = _json_body(status, body, step)
    code = data.get("code")
    if not code:
        raise AccountAuthError(f"{step}: 三星没有返回 code")

    async with session.get(
        f"{SERVICE_BASE}/getState.do", params={"payload": "hound"}
    ) as response:
        status = response.status
        body = await response.text()
    state = _json_body(status, body, "getState.do").get("state")

    auth_host = urlsplit(auth_server).hostname or ""
    async with session.get(
        f"{SERVICE_BASE}/login.do",
        params={
            "auth_server_url": auth_host,
            "api_server_url": auth_host,
            "code": str(code),
            "code_expires_in": str(data.get("code_expires_in", 300)),
            "state": str(state),
        },
        allow_redirects=False,
    ) as response:
        status = response.status
        body = await response.text()
    if status not in (200, 302):
        _json_body(status, body, "login.do")
        raise AccountAuthError(f"login.do: HTTP {status}")

    jsessionid = None
    for cookie in session.cookie_jar:
        if cookie.key == "JSESSIONID":
            jsessionid = cookie.value
    if not jsessionid:
        raise AccountAuthError("login.do 没有返回 JSESSIONID")
    _LOGGER.debug("Minted a new Samsung Find session")
    return jsessionid
