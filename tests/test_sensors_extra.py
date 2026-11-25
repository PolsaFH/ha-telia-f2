"""Tests for the throughput / WAN / client-count sensors and their parsing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telia_f2 import coordinator as coord_mod
from custom_components.telia_f2.api import TeliaF2ApiError, TeliaF2AuthError
from custom_components.telia_f2.const import DOMAIN
from custom_components.telia_f2.coordinator import (
    TeliaF2Coordinator,
    _parse_proc_net_dev,
)
from custom_components.telia_f2.sensor import (
    SIMPLE_SENSOR_DESCRIPTIONS,
    _count_clients,
    _wan_gateway,
    _wan_ipv6,
)

from .conftest import PASSWORD, USERNAME

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _desc(key: str):
    return next(d for d in SIMPLE_SENSOR_DESCRIPTIONS if d.key == key)


def test_parse_proc_net_dev_reads_wan_counters():
    stats = _fixture("status_statistics.json")
    interfaces = _parse_proc_net_dev(stats["statistics"])
    assert "br-wan" in interfaces
    assert interfaces["br-wan"]["rx_bytes"] == 247776551880
    assert interfaces["br-wan"]["tx_bytes"] == 14384538353
    # Header lines and malformed lines are skipped.
    assert "Inter-|" not in interfaces


def test_parse_proc_net_dev_handles_missing_or_garbage():
    assert _parse_proc_net_dev(None) == {}
    assert _parse_proc_net_dev("") == {}
    assert _parse_proc_net_dev("eth0: not enough columns") == {}


def _bare_coordinator() -> TeliaF2Coordinator:
    # Skip __init__ (needs hass); we only exercise the pure throughput math.
    c = object.__new__(TeliaF2Coordinator)
    c._prev_wan = None
    return c


def test_throughput_first_poll_has_totals_but_no_rate(monkeypatch):
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 1000.0)
    c = _bare_coordinator()
    interfaces = {"br-wan": {"rx_bytes": 1_000_000, "tx_bytes": 500_000}}
    out = c._compute_throughput(interfaces, {"device": "br-wan"})
    assert out["rx_bytes"] == 1_000_000
    assert out["tx_bytes"] == 500_000
    assert out["rx_mbps"] is None and out["tx_mbps"] is None


def test_throughput_second_poll_computes_rate(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: t["now"])
    c = _bare_coordinator()
    wan = {"l3_device": "br-wan"}
    c._compute_throughput({"br-wan": {"rx_bytes": 0, "tx_bytes": 0}}, wan)
    t["now"] = 1001.0  # one second later
    out = c._compute_throughput(
        {"br-wan": {"rx_bytes": 1_250_000, "tx_bytes": 2_500_000}}, wan
    )
    # 1_250_000 bytes/s * 8 = 10 Mbit/s; 2_500_000 * 8 = 20 Mbit/s
    assert out["rx_mbps"] == 10.0
    assert out["tx_mbps"] == 20.0


def test_throughput_counter_reset_yields_no_rate(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: t["now"])
    c = _bare_coordinator()
    wan = {"device": "br-wan"}
    c._compute_throughput({"br-wan": {"rx_bytes": 9_000_000, "tx_bytes": 9_000_000}}, wan)
    t["now"] = 1001.0
    # A reboot reset the counters backwards: no bogus spike.
    out = c._compute_throughput({"br-wan": {"rx_bytes": 10, "tx_bytes": 10}}, wan)
    assert out["rx_mbps"] is None and out["tx_mbps"] is None


def test_throughput_missing_wan_interface(monkeypatch):
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 1000.0)
    c = _bare_coordinator()
    out = c._compute_throughput({"eth0": {"rx_bytes": 1, "tx_bytes": 1}}, {"device": "br-wan"})
    assert out["rx_bytes"] is None and out["rx_mbps"] is None


def test_wan_gateway_from_default_route():
    wan = _fixture("status_wan.json")
    data = {"wan": wan}
    gw = _wan_gateway(data)
    assert gw == "192.0.2.1"  # the mask==0 next hop
    assert _wan_gateway({"wan": {}}) is None
    assert _wan_gateway({}) is None


def test_memory_used_sensor_reads_percent():
    data = {"dashboard": _fixture("dashboard.json")}
    assert _desc("memory_used").value_fn(data) == 75


def test_client_count_sensors():
    data = {
        "clients": [
            {"link_type": "Wi-Fi"},
            {"link_type": "Wi-Fi"},
            {"link_type": "Wire"},
        ]
    }
    assert _count_clients(data, "Wi-Fi") == 2
    assert _count_clients(data, "Wire") == 1
    assert _desc("wireless_clients").value_fn(data) == 2
    assert _desc("wired_clients").value_fn(data) == 1
    assert _count_clients({}, "Wi-Fi") == 0


def test_wan_uptime_sensor():
    data = {"wan": _fixture("status_wan.json")}
    assert _desc("wan_uptime").value_fn(data) == 427785


def test_throughput_rejects_nonstring_device(monkeypatch):
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 1000.0)
    c = _bare_coordinator()
    interfaces = {"br-wan": {"rx_bytes": 5, "tx_bytes": 7}}
    # A corrupt l3_device (a list) must not crash; fall back to br-wan.
    out = c._compute_throughput(interfaces, {"l3_device": ["br-wan"]})
    assert out["device"] == "br-wan"
    assert out["rx_bytes"] == 5 and out["tx_bytes"] == 7


def test_throughput_interface_change_resets_rate(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: t["now"])
    c = _bare_coordinator()
    interfaces = {
        "br-wan": {"rx_bytes": 0, "tx_bytes": 0},
        "wan1": {"rx_bytes": 10_000_000, "tx_bytes": 10_000_000},
    }
    c._compute_throughput(interfaces, {"device": "br-wan"})
    t["now"] = 1001.0
    # WAN device changed: no rate computed against the other interface.
    out = c._compute_throughput(interfaces, {"device": "wan1"})
    assert out["device"] == "wan1"
    assert out["rx_mbps"] is None and out["tx_mbps"] is None


def test_wan_gateway_handles_bad_route_shapes():
    assert _wan_gateway({"wan": {"route": 42}}) is None
    assert _wan_gateway({"wan": {"route": ["junk", 5]}}) is None
    assert _wan_gateway({"wan": {"route": [{"mask": 1, "nexthop": "x"}]}}) is None


def test_wan_ipv6_sensor():
    assert _wan_ipv6({"wan": _fixture("status_wan.json")}) is None  # empty on IPv4-only
    data = {"wan": {"ipv6-address": [{"address": "2001:db8::1"}]}}
    assert _desc("wan_ipv6").value_fn(data) == "2001:db8::1"
    assert _wan_ipv6({"wan": {"ipv6-address": "bad"}}) is None


async def test_optional_endpoint_propagates_auth_but_swallows_api_error():
    c = object.__new__(TeliaF2Coordinator)

    async def auth():
        raise TeliaF2AuthError("session rejected")

    async def api():
        raise TeliaF2ApiError("not found")

    with pytest.raises(TeliaF2AuthError):
        await c._optional(auth)
    assert await c._optional(api) is None


# --- Full integration: the sensors appear with the right states in HA ---


async def test_sensors_expose_router_data(hass, router, enable_custom_integrations):
    router.replies["/dashboard"] = {
        "result": {
            "cpu": {"all": 3},
            "mem": {"total": 2022564, "free": 498816, "used": 75},
            "uptime": {"uptime": "1 day"},
            "info": {"friendly_name": "Test Router", "model_name": "Telia F2"},
            "wan_status": {"wan": {"up": True, "ipv4-address": [{"address": "1.2.3.4"}]}},
            "interface": {},
        }
    }
    router.replies["/status/device"] = {
        "result": {
            "list": [
                {"type": "LAN", "hostname": "pc", "mac": "aa", "ip": "x", "rate": "1000"},
                {
                    "type": "Controller",
                    "hostname": "ctrl",
                    "mac": "bb",
                    "radios": [
                        {"stations": [{"hostname": "phone", "mac": "cc", "band": "5g"}]}
                    ],
                },
            ]
        }
    }
    router.replies["/status/statistics"] = {
        "result": {"statistics": _fixture("status_statistics.json")["statistics"]}
    }
    router.replies["/status/wan"] = {"result": _fixture("status_wan.json")}

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=router.host,
        data={CONF_HOST: router.host, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.test_router_memory_usage").state == "75"
    assert hass.states.get("sensor.test_router_wireless_clients").state == "1"
    assert hass.states.get("sensor.test_router_wired_clients").state == "1"
    assert hass.states.get("sensor.test_router_wan_gateway").state == "192.0.2.1"
    assert hass.states.get("sensor.test_router_wan_uptime").state == "427785"
    # Totals come from the br-wan interface counters.
    received = hass.states.get("sensor.test_router_wan_data_received")
    assert float(received.state) == pytest.approx(247776551880 / 1_000_000_000, abs=0.05)

    assert await hass.config_entries.async_unload(entry.entry_id)
