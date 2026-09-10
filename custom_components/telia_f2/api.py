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
  2. AES-256-CBC encrypt the plaintext password with a fixed key/IV that is
     hardcoded in the router's JS bundle (see const.py).
  3. POST /auth/login over the websocket with the encrypted password and
     the csrf token from step 1, using it as both the initial "sid" and
     "csrftoken" params.
  4. The login response returns a real "ubus_rpc_session" and "csrfToken"
     to use for every subsequent call. The session expires after 300s of
     inactivity.

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
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .const import (
    AES_IV_HEX,
    AES_KEY_HEX,
    SESSION_RENEW_MARGIN_SECONDS,
    SESSION_TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 HomeAssistant"
)


class TeliaF2AuthError(Exception):
    """Raised when login fails outright (bad password etc.)."""


class TeliaF2ApiError(Exception):
    """Raised when a call returns a jsonrpc error we could not recover from."""


def _encrypt_password(plaintext: str) -> str:
    key = bytes.fromhex(AES_KEY_HEX)
    iv = bytes.fromhex(AES_IV_HEX)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), 16))
    return base64.b64encode(ct).decode()


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
        self._login_time: float = 0.0
        self._id_counter = itertools.count(1)
        self._lock = asyncio.Lock()

    async def async_close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        self._sid = None
        self._csrftoken = None

    async def _fetch_csrf_token(self) -> str:
        url = f"{self._origin}/csrf-token"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self._origin + "/",
            "User-Agent": USER_AGENT,
        }
        async with self._session.get(url, headers=headers, timeout=10) as resp:
            token = resp.headers.get("X-CSRF-Token")
            if not token:
                raise TeliaF2AuthError(
                    "No X-CSRF-Token header in /csrf-token response"
                )
            return token

    async def _open_ws(self) -> aiohttp.ClientWebSocketResponse:
        url = f"ws://{self._host}/ws"
        headers = {
            "Origin": self._origin,
            "Referer": self._origin + "/",
            "User-Agent": USER_AGENT,
        }
        return await self._session.ws_connect(
            url,
            protocols=("jsonrpc",),
            headers=headers,
            heartbeat=30,
        )

    async def async_login(self) -> None:
        """Perform a full login: fetch csrf token, open ws, authenticate."""
        async with self._lock:
            await self._async_login_locked()

    async def _async_login_locked(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

        csrf_token = await self._fetch_csrf_token()
        self._ws = await self._open_ws()

        encrypted_pw = _encrypt_password(self._password)
        req_id = next(self._id_counter)
        login_payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "api",
            "params": {
                "path": "/auth/login",
                "action": "post",
                "msg": {
                    "username": self._username,
                    "password": encrypted_pw,
                    "csrfToken": csrf_token,
                },
                "sid": csrf_token,
                "csrftoken": csrf_token,
            },
        }
        await self._ws.send_json(login_payload)
        msg = await self._ws.receive_json(timeout=10)

        result = msg.get("result")
        if not result or result.get("code") != 0 or "ubus_rpc_session" not in result:
            error = msg.get("error", {})
            raise TeliaF2AuthError(
                f"Login failed: {error or msg}"
            )

        self._sid = result["ubus_rpc_session"]
        self._csrftoken = result["csrfToken"]
        self._login_time = time.monotonic()
        _LOGGER.debug("Telia F2: logged in, sid=%s", self._sid)

    def _session_needs_renewal(self) -> bool:
        if self._ws is None or self._ws.closed or self._sid is None:
            return True
        age = time.monotonic() - self._login_time
        return age > (SESSION_TIMEOUT_SECONDS - SESSION_RENEW_MARGIN_SECONDS)

    async def async_call(
        self, path: str, action: str = "get", msg: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call an API endpoint, logging in / renewing the session as needed."""
        if self._session_needs_renewal():
            await self.async_login()

        async with self._lock:
            return await self._async_call_locked(path, action, msg or {})

    async def _async_call_locked(
        self, path: str, action: str, msg: dict[str, Any]
    ) -> dict[str, Any]:
        assert self._ws is not None and self._sid is not None and self._csrftoken

        req_id = next(self._id_counter)
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "api",
            "params": {
                "path": path,
                "action": action,
                "msg": msg,
                "sid": self._sid,
                "csrftoken": self._csrftoken,
            },
        }
        await self._ws.send_json(payload)
        response = await self._ws.receive_json(timeout=10)

        if "error" in response:
            error = response["error"]
            if error.get("code") == 6:
                # Permission denied -- session likely stale. Re-login once
                # and retry a single time.
                _LOGGER.debug("Telia F2: session rejected, re-logging in")
                await self._async_login_locked()
                payload["params"]["sid"] = self._sid
                payload["params"]["csrftoken"] = self._csrftoken
                await self._ws.send_json(payload)
                response = await self._ws.receive_json(timeout=10)
                if "error" in response:
                    raise TeliaF2ApiError(f"{path}: {response['error']}")
            else:
                raise TeliaF2ApiError(f"{path}: {error}")

        return response.get("result", {})

    async def async_get_dashboard(self) -> dict[str, Any]:
        return await self.async_call("/dashboard")

    async def async_get_status_device(self) -> dict[str, Any]:
        return await self.async_call("/status/device")

    async def async_get_status_arp(self) -> dict[str, Any]:
        return await self.async_call("/status/arp")

    async def async_get_status_statistics(self) -> dict[str, Any]:
        return await self.async_call("/status/statistics")
