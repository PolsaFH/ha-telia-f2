"""Tests for the per-client device_tracker presence entities."""
from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telia_f2 import coordinator as coord_mod
from custom_components.telia_f2.const import CONSIDER_HOME_SECONDS, DOMAIN
from custom_components.telia_f2.coordinator import TeliaF2Coordinator

from .conftest import PASSWORD, USERNAME


def _bare():
    c = object.__new__(TeliaF2Coordinator)
    c._seen = {}
    c.data = {}
    return c


def test_is_home_consider_home_window(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: t["now"])
    c = _bare()
    c._seen["aa"] = 1000.0
    assert c.is_home("aa")
    t["now"] = 1000.0 + CONSIDER_HOME_SECONDS - 1
    assert c.is_home("aa")  # still within the grace window
    t["now"] = 1000.0 + CONSIDER_HOME_SECONDS + 1
    assert not c.is_home("aa")  # dropped off long enough -> away
    assert not c.is_home("never-seen")


def test_seen_macs_and_client_record():
    c = _bare()
    c._seen = {"aa": 1.0, "bb": 2.0}
    assert set(c.seen_macs()) == {"aa", "bb"}
    c.data = {"clients": [{"mac": "aa", "ip": "192.0.2.5", "hostname": "pc"}]}
    assert c.client_record("aa") == {"mac": "aa", "ip": "192.0.2.5", "hostname": "pc"}
    assert c.client_record("bb") is None  # known MAC, not connected right now


async def test_device_tracker_entities_created_and_home(
    hass, router, enable_custom_integrations
):
    router.replies["/dashboard"] = {
        "result": {
            "cpu": {"all": 1},
            "mem": {"used": 10},
            "info": {"friendly_name": "Test Router"},
            "interface": {},
        }
    }
    router.replies["/status/device"] = {
        "result": {
            "list": [
                {
                    "type": "LAN",
                    "hostname": "laptop",
                    "mac": "00:00:5e:00:53:01",
                    "ip": "192.0.2.20",
                    "rate": "1000",
                },
                {
                    "type": "Controller",
                    "hostname": "ctrl",
                    "mac": "00:00:5e:00:53:aa",
                    "radios": [
                        {
                            "stations": [
                                {
                                    "hostname": "phone",
                                    "mac": "00:00:5e:00:53:02",
                                    "ip": "192.0.2.21",
                                    "band": "5g",
                                }
                            ]
                        }
                    ],
                },
            ]
        }
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=router.host,
        data={CONF_HOST: router.host, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    laptop_id = ent_reg.async_get_entity_id(
        "device_tracker", DOMAIN, "00:00:5e:00:53:01"
    )
    phone_id = ent_reg.async_get_entity_id(
        "device_tracker", DOMAIN, "00:00:5e:00:53:02"
    )
    # A tracker is created per client...
    assert laptop_id == "device_tracker.laptop"
    assert phone_id == "device_tracker.phone"
    # ...but an unknown MAC is disabled by default (HA only auto-enables
    # trackers whose MAC another integration already knows).
    assert ent_reg.async_get(laptop_id).disabled_by is not None
    # The mesh node itself (Controller) is not a client, so it gets no tracker.
    assert ent_reg.async_get_entity_id("device_tracker", DOMAIN, "00:00:5e:00:53:aa") is None

    # Once enabled, it reports home while the router still lists it.
    ent_reg.async_update_entity(laptop_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    laptop = hass.states.get("device_tracker.laptop")
    assert laptop is not None and laptop.state == "home"
    assert laptop.attributes.get("ip") == "192.0.2.20"

    assert await hass.config_entries.async_unload(entry.entry_id)
