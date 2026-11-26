"""Tests for the Wi-Fi band / guest / IoT on-off switches."""
from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telia_f2.const import DOMAIN
from custom_components.telia_f2.switch import _enabled

from .conftest import PASSWORD, USERNAME


def _ssid(name, typ, band, enable):
    return {
        "name": name,
        "ssid": f"{typ}-{band}",
        "key": "a-fake-key",
        "encryption": "sae-mixed",
        "hidden": 0,
        "enable": enable,
        "type": typ,
        "band": band,
        "mlo": "0",
    }


def test_enabled_helper():
    assert _enabled({"enable": 1})
    assert _enabled({"enable": "1"})
    assert not _enabled({"enable": 0})
    assert not _enabled({"enable": "0"})
    assert not _enabled({})


async def _setup(hass, router):
    router.replies["/dashboard"] = {
        "result": {
            "cpu": {"all": 1},
            "mem": {"used": 5},
            "info": {"friendly_name": "Test Router"},
            "interface": {},
        }
    }
    router.ssids = [
        _ssid("default_wl2", "home", "2g", 1),
        _ssid("default_wl0", "home", "5g", 1),
        _ssid("default_wl1", "home", "6g", 1),
        _ssid("wifinet8", "guest", "2g", 0),
        _ssid("wifinet6", "guest", "5g", 0),
        _ssid("wifinet7", "guest", "6g", 0),
        _ssid("wifinet11", "iot", "2g", 0),
        _ssid("wifinet9", "iot", "5g", 0),
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=router.host,
        data={CONF_HOST: router.host, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_switches_created_with_correct_states(
    hass, router, enable_custom_integrations
):
    entry = await _setup(hass, router)
    reg = er.async_get(hass)
    band_2g = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_wifi_home_2g")
    band_5g = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_wifi_home_5g")
    band_6g = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_wifi_home_6g")
    guest = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_wifi_guest")
    iot = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_wifi_iot")
    assert all([band_2g, band_5g, band_6g, guest, iot])

    assert hass.states.get(band_2g).state == "on"
    assert hass.states.get(band_5g).state == "on"
    assert hass.states.get(band_6g).state == "on"
    assert hass.states.get(guest).state == "off"
    assert hass.states.get(iot).state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_turning_off_one_home_band_leaves_the_others(
    hass, router, enable_custom_integrations
):
    entry = await _setup(hass, router)
    reg = er.async_get(hass)
    band_2g = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_wifi_home_2g")

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": band_2g}, blocking=True
    )
    await hass.async_block_till_done()

    by_name = {s["name"]: s for s in router.ssids}
    assert by_name["default_wl2"]["enable"] == "0"  # 2.4 GHz turned off
    assert by_name["default_wl0"]["enable"] == 1  # 5 GHz untouched
    assert by_name["default_wl1"]["enable"] == 1  # 6 GHz untouched
    assert hass.states.get(band_2g).state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_turning_on_guest_enables_all_its_bands(
    hass, router, enable_custom_integrations
):
    entry = await _setup(hass, router)
    reg = er.async_get(hass)
    guest = reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_wifi_guest")

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": guest}, blocking=True
    )
    await hass.async_block_till_done()

    guest_entries = [s for s in router.ssids if s["type"] == "guest"]
    assert all(s["enable"] == "1" for s in guest_entries)
    # The main network is left alone.
    assert all(s["enable"] == 1 for s in router.ssids if s["type"] == "home")
    assert hass.states.get(guest).state == "on"

    assert await hass.config_entries.async_unload(entry.entry_id)
