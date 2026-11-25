"""Tests for setup, polling and the config flow inside Home Assistant."""
from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telia_f2.const import DOMAIN
from custom_components.telia_f2.coordinator import MAX_AUTH_FAILURES

from .conftest import PASSWORD, USERNAME


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components."""


def _entry(hass, router, password=PASSWORD):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Telia F2 ({router.host})",
        unique_id=router.host,
        data={
            CONF_HOST: router.host,
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: password,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _reauth_flows(hass):
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == config_entries.SOURCE_REAUTH
    ]


async def _setup(hass, entry):
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_existing_entry_loads_with_the_stored_password(hass, router):
    entry = _entry(hass, router)
    await _setup(hass, entry)
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_wrong_password_can_be_replaced(hass, router):
    entry = _entry(hass, router, "wrong")
    await _setup(hass, entry)
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    (flow,) = _reauth_flows(hass)

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "still wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_PASSWORD] == PASSWORD
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unreachable_router_is_retried_without_reauth(hass, router):
    entry = _entry(hass, router)
    await router.stop()
    await _setup(hass, entry)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass)


async def test_unexpected_login_answer_is_not_a_wrong_password(hass, router):
    router.send_csrf = False
    entry = _entry(hass, router)
    await _setup(hass, entry)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY
    assert not _reauth_flows(hass)


async def test_password_changed_while_running_asks_for_it(hass, router):
    entry = _entry(hass, router)
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    router.password = "changed on the router"
    for attempt in range(1, MAX_AUTH_FAILURES + 1):
        router.expire_sessions()
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert not coordinator.last_update_success
        assert bool(_reauth_flows(hass)) is (attempt == MAX_AUTH_FAILURES)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_user_flow(hass, router):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    data = {CONF_HOST: router.host, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**data, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == data
    await hass.async_block_till_done()

    entry = result["result"]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data=data
    )
    assert result["reason"] == "already_configured"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"rate_limited": True}, "too_many_attempts"),
        ({"send_csrf": False}, "unexpected_response"),
        ({"host": "127.0.0.1:1"}, "cannot_connect"),
    ],
)
async def test_user_flow_errors(hass, router, change, error):
    for key, value in change.items():
        setattr(router, key, value)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
        data={
            CONF_HOST: router.host,
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
