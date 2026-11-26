"""A fake Telia F2 router, speaking the same protocol as the real one."""
from __future__ import annotations

import base64
import secrets
from typing import Any

import pytest
import pytest_socket
from aiohttp import WSMsgType, web
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import unpad

from custom_components.telia_f2.const import AES_IV_HEX, AES_KEY_HEX

USERNAME = "admin"
PASSWORD = "correct horse æøå"
RSA_KEY = RSA.generate(2048)


class FakeRouter:
    """Implements csrf-token, challenge, login and a few read endpoints."""

    def __init__(self, legacy: bool = False) -> None:
        self.legacy = legacy
        self.password = PASSWORD
        self.host = ""
        self.logins = 0
        self.login_attempts = 0
        self.calls: list[str] = []
        self.rate_limited = False
        self.send_csrf = True
        self.drop_next: str | None = None  # close the ws on this path, once
        self.deny_next: int | None = None  # answer a call with this code, once
        self.push_before_reply = False
        self.replies: dict[str, dict[str, Any]] = {}  # fixed reply per path
        self.ssids: list[dict[str, Any]] = []  # committed Wi-Fi SSID entries
        self.pending: dict[str, Any] = {}  # staged by PUT, applied on commit
        self.put_payloads: list[dict[str, Any]] = []  # every PUT body received
        self.fail_put_names: set[str] = set()  # inject a write failure
        self._csrf: set[str] = set()
        self._nonces: dict[str, str] = {}
        self._sessions: dict[str, str] = {}
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/csrf-token", self._handle_csrf)
        app.router.add_get("/ws", self._handle_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.host = f"127.0.0.1:{self._runner.addresses[0][1]}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def expire_sessions(self) -> None:
        self._sessions.clear()

    async def _handle_csrf(self, request: web.Request) -> web.Response:
        token = secrets.token_hex(16)
        self._csrf.add(token)
        headers = {"X-CSRF-Token": token} if self.send_csrf else {}
        return web.Response(text="placeholder", headers=headers)

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Origin") != f"http://{self.host}":
            return web.Response(status=403)  # like the real router
        ws = web.WebSocketResponse(protocols=("jsonrpc",))
        await ws.prepare(request)
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                break
            data = message.json()
            params = data["params"]
            path = params["path"]
            self.calls.append(path)
            if path == self.drop_next:
                self.drop_next = None
                await ws.close()
                break
            if self.push_before_reply:
                await ws.send_json({"jsonrpc": "2.0", "method": "event", "params": {}})
            reply = self._reply(path, params)
            await ws.send_json({"jsonrpc": "2.0", "id": data["id"], **reply})
        return ws

    def _reply(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        msg = params["msg"]
        if path in self.replies:
            if path == "/auth/login":
                self.login_attempts += 1
            return self.replies[path]
        if path == "/auth/challenge":
            if self.legacy:
                # How the real router answers an endpoint it does not have.
                return _error(4, "Not found")
            if self.rate_limited:
                return _error(7, "Too many attempts")
            if msg.get("csrfToken") not in self._csrf:
                return _error(2, "Invalid argument")
            nonce = secrets.token_hex(16)
            self._nonces[nonce] = msg["csrfToken"]
            public_key = RSA_KEY.public_key().export_key().decode()
            return {
                "result": {"publicKey": public_key, "nonce": nonce, "expiresIn": 60}
            }
        if path == "/auth/login":
            return self._login(msg)
        if self._sessions.get(params["sid"]) != params["csrftoken"]:
            return _error(6, "Permission denied")
        if self.deny_next is not None:
            code, self.deny_next = self.deny_next, None
            return _error(code, "error")
        if path == "/status/device":
            return {"result": {"list": []}}
        if path == "/airties/ssid/all":
            # Reads see the committed state only.
            return {"result": {"list": self.ssids}}
        if path == "/airties/ssid":
            # A write stages a change; it is not visible until commit.
            self.put_payloads.append(msg)
            name = msg.get("name")
            if name in self.fail_put_names:
                return _error(15, "error")
            self.pending[name] = msg.get("enable")
            return {"result": {"code": 0}}
        if path == "/commit":
            for ssid in self.ssids:
                if ssid.get("name") in self.pending:
                    ssid["enable"] = self.pending[ssid["name"]]
            self.pending.clear()
            return {"result": {"code": 0}}
        return {"result": {"path": path, "cpu": {"all": 7}, "interface": {}}}

    def _login(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.login_attempts += 1
        if self.rate_limited:
            return _error(7, "Too many attempts")
        if msg.get("csrfToken") not in self._csrf:
            return _error(6, "Permission denied")
        encrypted = base64.b64decode(msg["password"])
        if self.legacy:
            key, iv = bytes.fromhex(AES_KEY_HEX), bytes.fromhex(AES_IV_HEX)
            plain = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(encrypted), 16)
        else:
            # The real router answers the old login (no nonce) with code 2.
            if self._nonces.pop(msg.get("nonce", ""), None) != msg["csrfToken"]:
                return _error(2, "Invalid argument")
            plain = PKCS1_v1_5.new(RSA_KEY).decrypt(encrypted, b"")
        if msg["username"] != USERNAME or plain.decode() != self.password:
            return _error(6 if self.legacy else 5, "Invalid credentials")
        sid, csrf = secrets.token_hex(16), secrets.token_hex(16)
        self._sessions[sid] = csrf
        self.logins += 1
        return {
            "result": {"username": USERNAME, "ubus_rpc_session": sid, "csrfToken": csrf}
        }


def _error(code: int, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


@pytest.fixture(autouse=True)
def _allow_local_sockets(socket_enabled):
    """The fake router listens on 127.0.0.1."""
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)


@pytest.fixture
async def router(_allow_local_sockets):
    fake = FakeRouter()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def legacy_router(_allow_local_sockets):
    fake = FakeRouter(legacy=True)
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def expected_lingering_timers() -> bool:
    """aiohttp may leave a heartbeat timer behind after a websocket closes.

    The websocket itself is closed, and the timer is gone within a minute.
    """
    return True
