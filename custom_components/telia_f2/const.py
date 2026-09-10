"""Constants for the Telia F2 router integration."""

DOMAIN = "telia_f2"

DEFAULT_HOST = "192.168.1.1"
DEFAULT_USERNAME = "admin"

# Polling interval. The router session times out after 300s of inactivity,
# so we poll well within that window and also proactively re-login before
# the timeout to keep a single long-lived session alive.
UPDATE_INTERVAL_SECONDS = 30
SESSION_TIMEOUT_SECONDS = 300
SESSION_RENEW_MARGIN_SECONDS = 60  # re-login this many seconds before timeout

# AES-256-CBC key/IV used by the router's web UI to encrypt the login
# password client-side. Found by hooking the CryptoJS.AES.encrypt() call
# in the router's own JS bundle (app.*.js) during a real login.
AES_KEY_HEX = "3030373230386536623966663534653937346330383633353339376231326634"
AES_IV_HEX = "64623032303530363039663930376139"
