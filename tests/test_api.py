"""Tests for the router client."""
from __future__ import annotations

import aiohttp
import pytest

from custom_components.telia_f2 import api
from custom_components.telia_f2.api import (
    TeliaF2ApiError,
    TeliaF2AuthError,
    TeliaF2Client,
    TeliaF2ConnectionError,
    TeliaF2RateLimitError,
)

from .conftest import PASSWORD, USERNAME


@pytest.fixture
async def make_client():
    session = aiohttp.ClientSession()
    clients: list[TeliaF2Client] = []

    def make(router, password=PASSWORD):
        client = TeliaF2Client(session, router.host, USERNAME, password)
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.async_close()
    await session.close()


async def test_login_with_challenge(router, make_client):
    client = make_client(router)
    await client.async_login()
    assert router.logins == 1
    assert (await client.async_get_dashboard())["path"] == "/dashboard"


async def test_login_on_older_firmware(legacy_router, make_client):
    client = make_client(legacy_router)
    await client.async_login()
    assert (await client.async_get_dashboard())["path"] == "/dashboard"


async def test_wrong_password(router, make_client):
    with pytest.raises(TeliaF2AuthError):
        await make_client(router, "wrong").async_login()
    assert router.login_attempts == 1


async def test_wrong_password_on_older_firmware(legacy_router, make_client):
    with pytest.raises(TeliaF2AuthError):
        await make_client(legacy_router, "wrong").async_login()
    assert legacy_router.login_attempts == 1


async def test_login_format_error_is_not_a_wrong_password(router, make_client):
    """Code 2 is what the old login got after the firmware update."""
    router._nonces = _ForgetfulDict()
    with pytest.raises(TeliaF2ApiError, match="invalid argument"):
        await make_client(router).async_login()


@pytest.mark.parametrize("code", [3, 4])
async def test_old_login_only_without_challenge(legacy_router, make_client, code):
    legacy_router.replies["/auth/challenge"] = {"error": {"code": code}}
    await make_client(legacy_router).async_login()
    assert legacy_router.logins == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"error": {"code": 2, "message": "Invalid argument"}},
        {"error": {"code": 6, "message": "Permission denied"}},
        {"error": {"code": 14, "message": "Max sessions"}},
        {"error": {"code": ["unhashable"]}},
        {"error": "text"},
        {"result": {}},
        {"result": {"publicKey": "-----BEGIN PUBLIC KEY-----"}},
        {"result": {"publicKey": "not a key", "nonce": "abc"}},
    ],
)
async def test_failed_challenge_never_tries_the_old_login(router, make_client, reply):
    router.replies["/auth/challenge"] = reply
    with pytest.raises(TeliaF2ApiError):
        await make_client(router).async_login()
    assert router.login_attempts == 0


@pytest.mark.parametrize("path", ["/auth/challenge", "/auth/login", "/dashboard"])
@pytest.mark.parametrize("code", [2, 5, 7, 99, "SECRET", None])
async def test_errors_do_not_repeat_what_the_router_sent(
    router, make_client, caplog, path, code
):
    caplog.set_level("DEBUG")
    router.replies[path] = {
        "error": {
            "code": code,
            "message": "SECRET",
            "data": {"csrfToken": "SECRET", "password": PASSWORD},
        }
    }
    with pytest.raises((TeliaF2ApiError, TeliaF2AuthError)) as caught:
        await make_client(router).async_get_dashboard()
    for text in (str(caught.value), repr(caught.value), caplog.text):
        assert "SECRET" not in text
        assert PASSWORD not in text


async def test_handshake_errors_do_not_repeat_headers(router, make_client):
    client = make_client(router)
    client._origin = router.host  # not an origin the router accepts
    with pytest.raises(TeliaF2ConnectionError) as caught:
        await client._open_ws()
    assert "403" in str(caught.value)
    assert "eaders" not in str(caught.value)


async def test_rate_limit(router, make_client):
    router.rate_limited = True
    with pytest.raises(TeliaF2RateLimitError):
        await make_client(router).async_login()
    assert router.login_attempts == 0


async def test_missing_csrf_token_is_not_a_wrong_password(router, make_client):
    router.send_csrf = False
    with pytest.raises(TeliaF2ApiError):
        await make_client(router).async_login()


async def test_unreachable_router(router, make_client):
    await router.stop()
    with pytest.raises(TeliaF2ConnectionError):
        await make_client(router).async_login()


async def test_polling_keeps_one_session(router, make_client, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(api.time, "monotonic", lambda: now)
    client = make_client(router)
    for _ in range(40):  # 20 minutes of polling
        await client.async_get_dashboard()
        now += 30
    assert router.logins == 1
    now += 300
    await client.async_get_dashboard()
    assert router.logins == 2


@pytest.mark.parametrize("code", [6, 14])
async def test_lost_session_logs_in_again(router, make_client, code):
    client = make_client(router)
    await client.async_get_dashboard()
    router.deny_next = code
    await client.async_get_dashboard()
    assert router.logins == 2


async def test_expired_session_logs_in_again(router, make_client):
    client = make_client(router)
    await client.async_get_dashboard()
    router.expire_sessions()
    await client.async_get_dashboard()
    assert router.logins == 2


async def test_dropped_connection_is_retried_for_reads(router, make_client):
    client = make_client(router)
    await client.async_get_dashboard()
    router.drop_next = "/dashboard"
    await client.async_get_dashboard()
    assert router.logins == 2


async def test_dropped_connection_never_repeats_a_reboot(router, make_client):
    client = make_client(router)
    await client.async_login()
    router.drop_next = "/admin/reboot"
    with pytest.raises(TeliaF2ConnectionError):
        await client.async_call("/admin/reboot", action="post")
    assert router.calls.count("/admin/reboot") == 1


@pytest.mark.parametrize("code", [6, 14])
async def test_rejected_session_never_repeats_a_reboot(router, make_client, code):
    client = make_client(router)
    await client.async_login()
    router.deny_next = code
    with pytest.raises(TeliaF2ApiError):
        await client.async_call("/admin/reboot", action="post")
    assert router.calls.count("/admin/reboot") == 1
    assert router.logins == 1

    await client.async_get_dashboard()
    assert router.logins == 2


async def test_unsolicited_messages_are_skipped(router, make_client):
    router.push_before_reply = True
    client = make_client(router)
    assert (await client.async_get_dashboard())["path"] == "/dashboard"


class _ForgetfulDict(dict):
    """Makes the router reject every nonce."""

    def __setitem__(self, key, value):
        pass
