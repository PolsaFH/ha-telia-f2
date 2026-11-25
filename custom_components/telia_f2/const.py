"""Constants for the Telia F2 router integration."""

DOMAIN = "telia_f2"

DEFAULT_HOST = "192.168.1.1"
DEFAULT_USERNAME = "admin"

# Polling interval. The router session times out after 300s of inactivity,
# so polling well within that window keeps a single session alive. We only
# log in again when the session has been idle for too long.
UPDATE_INTERVAL_SECONDS = 30
SESSION_TIMEOUT_SECONDS = 300
SESSION_RENEW_MARGIN_SECONDS = 60  # re-login this many seconds before timeout
REQUEST_TIMEOUT_SECONDS = 10

# AES-256-CBC key/IV used by the web UI of older firmware (v2.4.18 and
# earlier) to encrypt the login password client-side. Newer firmware uses a
# one-time RSA challenge instead (see api.py). Found by hooking the
# CryptoJS.AES.encrypt() call in the router's own JS bundle (app.*.js) during
# a real login.
AES_KEY_HEX = "3030373230386536623966663534653937346330383633353339376231326634"
AES_IV_HEX = "64623032303530363039663930376139"

# A device-tracker stays "home" for this long after it was last seen in the
# router's client list, so a phone that briefly sleeps its Wi-Fi radio does
# not flap to "away" (6 missed 30s polls).
CONSIDER_HOME_SECONDS = 180
