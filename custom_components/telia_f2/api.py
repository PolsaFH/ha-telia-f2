"""Client for the Telia F2 (Kaon) router's local jsonrpc-over-websocket API.

Reverse engineered from the router's own web UI (app.*.js). The router
exposes a websocket at ws://<host>/ws with subprotocol "jsonrpc". All calls
are wrapped as:

    {"jsonrpc":"2.0","id":N,"method":"api","params":{
        "path": "<endpoint>", "action": "get"|"post", "msg": {...},
        "sid": "<ubus_rpc_session>", "csrftoken": "<csrfToken>"
    }}

Login requires:
  1. GET http://<host>/csrf-token -> real CSRF token in the X-CSRF-Token
     response header (the response body is just a placeholder string, the
     token is NOT in the body).
  2. GET /auth/challenge over the websocket with the csrf token. Returns a
     one-time "nonce" and an RSA "publicKey" (PEM).
  3. RSA encrypt (PKCS#1 v1.5, like JSEncrypt in the web UI) the plaintext
     password with that public key and base64 encode it.
  4. POST /auth/login over the websocket with the encrypted password, the
     nonce and the csrf token, using the csrf token as both the initial
     "sid" and "csrftoken" params.
  5. The login response returns a real "ubus_rpc_session" and "csrfToken"
     to use for every subsequent call. The session expires after 300s of
     inactivity.

Older firmware (v2.4.18 and earlier) has no /auth/challenge. There the
password is AES-256-CBC encrypted with a fixed key/IV from the router's JS
bundle (see const.py) and sent without a nonce. Newer firmware answers that
old login with {"code": 2, "message": "Invalid argument"}.

Without a real token from /csrf-token, login still returns code 0 (looks
successful) but every subsequent call fails with error code 6
("Permission denied") -- the session is authenticated but not authorized.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import time
from typing import Any

import aiohttp
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

from .const import (
    AES_IV_HEX,
    AES_KEY_HEX,
    REQUEST_TIMEOUT_SECONDS,
    SESSION_RENEW_MARGIN_SECONDS,
    SESSION_TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 HomeAssistant"
)

# Error codes, as interpreted by the router's own web UI.
ERROR_INVALID_CREDENTIALS = 5
ERROR_PERMISSION_DENIED = 6  # "username or password is incorrect" on login
ERROR_TOO_MANY_ATTEMPTS = 7
ERROR_MAX_SESSIONS = 14  # our session was dropped by the router

LOGIN_AUTH_ERRORS = {ERROR_INVALID_CREDENTIALS, ERROR_PERMISSION_DENIED}
SESSION_LOST_ERRORS = {ERROR_PERMISSION_DENIED, ERROR_MAX_SESSIONS}


class TeliaF2AuthError(Exception):
    """Raised when the router rejects the username or password."""


class TeliaF2ApiError(Exception):
    """Raised when a call returns a jsonrpc error we could not recover from."""


class TeliaF2ConnectionError(TeliaF2ApiError):
    """Raised when the router can't be reached or drops the connection."""


class TeliaF2RateLimitError(TeliaF2ApiError):
    """Raised when the router blocks logins after too many attempts."""


def _encrypt_password_aes(plaintext: str) -> str:
    """Encrypt the password the way older firmware expects it."""
    key = bytes.fromhex(AES_KEY_HEX)
    iv = bytes.fromhex(AES_IV_HEX)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), 16))
    return base64.b64encode(ct).decode()


def _encrypt_password_rsa(plaintext: str, public_key_pem: str) -> str:
    """Encrypt the password with the public key from /auth/challenge."""
    try:
        cipher = PKCS1_v1_5.new(RSA.import_key(public_key_pem))
        return base64.b64encode(cipher.encrypt(plaintext.encode("utf-8"))).decode()
    except (ValueError, IndexError, TypeError) as err:
        raise TeliaF2ApiError(f"Could not encrypt the password: {err}") from err


def _error_code(response: dict[str, Any]) -> int | None:
    """Return the jsonrpc error code of a response, if it is an error."""
    error = response.get("error")
    return error.get("code") if isinstance(error, dict) else None


class TeliaF2Client:
    """Handles login, session renewal, and jsonrpc calls to the router."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._host = host
        self._username = username
        self._password = password

        self._origin = f"http://{host}"
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._sid: str | None = None
        self._csrftoken: str | None = None
        self._last_activity: float = 0.0
        self._id_counter = itertools.count(1)
        self._lock = asyncio.Lock()

    async def async_close(self) -> None:
        ws, self._ws = self._ws, None
        self._sid = None
        self._csrftoken = None
        if ws is not None and not ws.closed:
            await ws.close()

    async def _fetch_csrf_token(self) -> str:
        url = f"{self._origin}/csrf-token"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self._origin + "/",
            "User-Agent": USER_AGENT,
        }
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.get(url, headers=headers) as resp:
                    token = resp.headers.get("X-CSRF-Token")
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise TeliaF2ConnectionError(
                f"Could not fetch /csrf-token: {err!r}"
            ) from err
        if not token:
            raise TeliaF2ApiError("No X-CSRF-Token header in /csrf-token response")
        return token

    async def _open_ws(self) -> aiohttp.ClientWebSocketResponse:
        url = f"ws://{self._host}/ws"
        headers = {
            "Origin": self._origin,
            "Referer": self._origin + "/",
            "User-Agent": USER_AGENT,
        }
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                return await self._session.ws_connect(
                    url,
                    protocols=("jsonrpc",),
                    headers=headers,
                    heartbeat=30,
                )
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise TeliaF2ConnectionError(f"Could not open websocket: {err!r}") from err

    async def _async_request(
        self,
        path: str,
        action: str,
        msg: dict[str, Any],
        sid: str,
        csrftoken: str,
    ) -> dict[str, Any]:
        """Send one request and return the response that belongs to it."""
        ws = self._ws
        if ws is None or ws.closed:
            raise TeliaF2ConnectionError(f"{path}: websocket is not open")

        req_id = next(self._id_counter)
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "api",
            "params": {
                "path": path,
                "action": action,
                "msg": msg,
                "sid": sid,
                "csrftoken": csrftoken,
            },
        }
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                await ws.send_json(payload)
                while True:
                    message = await ws.receive()
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        raise TeliaF2ConnectionError(
                            f"{path}: websocket closed ({message.type.name})"
                        )
                    response = message.json()
                    if not isinstance(response, dict):
                        continue
                    # The router can push messages of its own. Errors without
                    # an id concern the whole session, so they end this call.
                    response_id = response.get("id")
                    if response_id == req_id or (
                        response_id is None and response.get("error")
                    ):
                        return response
        except TimeoutError as err:
            raise TeliaF2ConnectionError(f"{path}: no response in time") from err
        except (aiohttp.ClientError, OSError, ValueError) as err:
            raise TeliaF2ConnectionError(f"{path}: {err!r}") from err

    async def async_login(self) -> None:
        """Perform a full login: csrf token, websocket, challenge, login."""
        async with self._lock:
            await self._async_login_locked()

    async def _async_login_locked(self) -> None:
        await self.async_close()
        try:
            csrf_token = await self._fetch_csrf_token()
            self._ws = await self._open_ws()
            credentials = await self._async_build_credentials(csrf_token)
            response = await self._async_request(
                "/auth/login",
                "post",
                {
                    "username": self._username,
                    **credentials,
                    "csrfToken": csrf_token,
                },
                sid=csrf_token,
                csrftoken=csrf_token,
            )
            self._store_session(response)
        except BaseException:
            await self.async_close()
            raise

    async def _async_build_credentials(self, csrf_token: str) -> dict[str, str]:
        """Return the password fields for /auth/login."""
        response = await self._async_request(
            "/auth/challenge",
            "get",
            {"csrfToken": csrf_token},
            sid=csrf_token,
            csrftoken=csrf_token,
        )
        if _error_code(response) == ERROR_TOO_MANY_ATTEMPTS:
            raise TeliaF2RateLimitError("Too many login attempts")

        result = response.get("result")
        if isinstance(result, dict):
            public_key = result.get("publicKey")
            nonce = result.get("nonce")
            if public_key and nonce:
                return {
                    "password": _encrypt_password_rsa(self._password, public_key),
                    "nonce": nonce,
                }

        _LOGGER.debug("Telia F2: no login challenge, using the old login")
        return {"password": _encrypt_password_aes(self._password)}

    def _store_session(self, response: dict[str, Any]) -> None:
        """Store the session from a login response, or raise."""
        # NOTE: successful responses don't include a "code" key at all (only
        # failed logins/permission errors do, via the top-level "error"
        # object). The only reliable success signal is the presence of a real
        # session + csrf token.
        result = response.get("result")
        if (
            isinstance(result, dict)
            and result.get("ubus_rpc_session")
            and result.get("csrfToken")
        ):
            self._sid = result["ubus_rpc_session"]
            self._csrftoken = result["csrfToken"]
            self._last_activity = time.monotonic()
            _LOGGER.debug("Telia F2: logged in")
            return

        code = _error_code(response)
        # Never include the full response: it could hold session secrets.
        message = f"Login failed: {response.get('error') or 'unexpected response'}"
        if code in LOGIN_AUTH_ERRORS:
            raise TeliaF2AuthError(message)
        if code == ERROR_TOO_MANY_ATTEMPTS:
            raise TeliaF2RateLimitError(message)
        raise TeliaF2ApiError(message)

    def _session_needs_renewal(self) -> bool:
        if self._ws is None or self._ws.closed or not self._sid or not self._csrftoken:
            return True
        # The router ends a session after a period of inactivity, so a session
        # that is polled regularly does not need a new login.
        idle = time.monotonic() - self._last_activity
        return idle > (SESSION_TIMEOUT_SECONDS - SESSION_RENEW_MARGIN_SECONDS)

    async def async_call(
        self, path: str, action: str = "get", msg: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call an API endpoint, logging in / renewing the session as needed."""
        async with self._lock:
            try:
                return await self._async_call_locked(path, action, msg or {})
            except TeliaF2ConnectionError:
                # Never repeat a write: the router may have acted on it already.
                if action != "get":
                    raise
                _LOGGER.debug("Telia F2: connection lost, retrying %s", path)
                await self.async_close()
                return await self._async_call_locked(path, action, msg or {})

    async def _async_call_locked(
        self, path: str, action: str, msg: dict[str, Any]
    ) -> dict[str, Any]:
        if self._session_needs_renewal():
            await self._async_login_locked()

        response = await self._async_session_request(path, action, msg)
        if _error_code(response) in SESSION_LOST_ERRORS:
            # The session is stale. Log in again and retry a single time.
            _LOGGER.debug("Telia F2: session rejected, logging in again")
            await self._async_login_locked()
            response = await self._async_session_request(path, action, msg)

        if response.get("error"):
            raise TeliaF2ApiError(f"{path}: {response['error']}")

        return response.get("result") or {}

    async def _async_session_request(
        self, path: str, action: str, msg: dict[str, Any]
    ) -> dict[str, Any]:
        assert self._sid is not None and self._csrftoken is not None
        try:
            response = await self._async_request(
                path, action, msg, sid=self._sid, csrftoken=self._csrftoken
            )
        except BaseException:
            # A late answer must not leak into the next request.
            await self.async_close()
            raise
        self._last_activity = time.monotonic()
        return response

    async def async_get_dashboard(self) -> dict[str, Any]:
        return await self.async_call("/dashboard")

    async def async_get_status_device(self) -> dict[str, Any]:
        return await self.async_call("/status/device")

    async def async_get_status_arp(self) -> dict[str, Any]:
        return await self.async_call("/status/arp")

    async def async_get_status_statistics(self) -> dict[str, Any]:
        return await self.async_call("/status/statistics")
