DOMAIN = "smartthings_find"

# Base URL of the China Samsung Find web service
SERVICE_BASE = "https://findmymobile.samsung.cn"

# --- config entry data -------------------------------------------------------
CONF_AUTH_METHOD = "auth_method"
CONF_COOKIE = "cookie"
CONF_ACCOUNT = "account"
# Kept for compatibility with entries created by earlier versions
CONF_JSESSIONID = "jsessionid"

AUTH_METHOD_COOKIE = "cookie"
AUTH_METHOD_ACCOUNT = "samsung_account"

#: Field of the account flow: the ms-app:// callback the user pasted
CONF_CALLBACK_URL = "callback_url"
#: Read-only-ish field that only exists so the sign-in URL can be copied
CONF_SIGNIN_URL = "signin_url"
#: Device name sent to Samsung (shown in the account's device list)
CONF_DEVICE_NAME = "device_name"

# --- options -----------------------------------------------------------------
CONF_ACTIVE_MODE_SMARTTAGS = "active_mode_smarttags"
CONF_ACTIVE_MODE_OTHERS = "active_mode_others"

CONF_ACTIVE_MODE_SMARTTAGS_DEFAULT = True
CONF_ACTIVE_MODE_OTHERS_DEFAULT = False

CONF_UPDATE_INTERVAL = "update_interval"
CONF_UPDATE_INTERVAL_DEFAULT = 300
CONF_UPDATE_INTERVAL_MIN = 30

#: Minimum time between two automatic location requests to the same device
CONF_ACTIVE_INTERVAL = "active_interval"
CONF_ACTIVE_INTERVAL_DEFAULT = 300
CONF_ACTIVE_INTERVAL_MIN = 60

#: Minutes after which the tracker stops reporting an old position (0 = keep)
CONF_MAX_LOCATION_AGE = "max_location_age"
CONF_MAX_LOCATION_AGE_DEFAULT = 0

#: Repair issue shown when Samsung does not allow a device to report positions
ISSUE_LOCATION_NOT_ALLOWED = "location_not_allowed"

#: Minimum time between two attempts to mint a new web session
SESSION_RENEW_MIN_INTERVAL = 60

BATTERY_LEVELS = {
    'FULL': 100,
    'MEDIUM': 50,
    'LOW': 15,
    'VERY_LOW': 5
}
