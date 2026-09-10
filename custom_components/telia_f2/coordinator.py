"""Coordinator that polls the Telia F2 router and normalizes the data.

/status/device returns a flat "list" that mixes two kinds of entries:
  - Mesh nodes (type "Controller" or "Extender"): these have their own
    "radios" -> "stations" nesting, listing the WIRELESS clients connected
    to THAT specific node. The node entry itself also carries top-level
    band/rssi/noise/rate fields for an Extender, which is that extender's
    own backhaul link quality back to the Controller.
  - Wired clients (type "LAN"): flat entries with hostname/mac/ip/rate
    directly, not nested under anything.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TeliaF2ApiError, TeliaF2AuthError, TeliaF2Client
from .const import DOMAIN, UPDATE_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

NODE_TYPES = {"Controller", "Extender"}


class TeliaF2Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls /dashboard and /status/device and merges into one data dict."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: TeliaF2Client
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            dashboard = await self.client.async_get_dashboard()
            status_device = await self.client.async_get_status_device()
        except TeliaF2AuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except TeliaF2ApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

        nodes, clients = _process_status_device(status_device)
        ports = _process_ethernet_ports(dashboard)

        return {
            "dashboard": dashboard,
            "nodes": nodes,
            "clients": clients,
            "ports": ports,
        }


def _process_status_device(
    status_device: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Split /status/device's flat list into mesh nodes and end-clients."""
    nodes: dict[str, dict[str, Any]] = {}
    clients: list[dict[str, Any]] = []

    for entry in status_device.get("list", []):
        entry_type = entry.get("type")

        if entry_type in NODE_TYPES:
            node_key = entry.get("mac", entry.get("hostname"))
            nodes[node_key] = {
                "hostname": entry.get("hostname"),
                "mac": entry.get("mac"),
                "ip": entry.get("ip"),
                "type": entry_type,
                "serial_number": entry.get("sn"),
                "firmware_version": entry.get("ver"),
                # For an Extender, these top-level fields are its own
                # backhaul link quality back to the Controller. They read
                # as "-" (unused) on the Controller itself.
                "backhaul_band": entry.get("band"),
                "backhaul_rssi": entry.get("rssi"),
                "backhaul_rate": entry.get("rate"),
            }
            for radio in entry.get("radios", []):
                for station in radio.get("stations", []):
                    clients.append(
                        {
                            "hostname": station.get("hostname"),
                            "mac": station.get("mac"),
                            "ip": station.get("ip"),
                            "link_type": "Wi-Fi",
                            "band": station.get("band"),
                            "ssid": station.get("ssid"),
                            "rssi": station.get("rssi"),
                            "rate": station.get("rate"),
                            "connected_via": entry.get("hostname"),
                        }
                    )
        elif entry_type == "LAN":
            clients.append(
                {
                    "hostname": entry.get("hostname"),
                    "mac": entry.get("mac"),
                    "ip": entry.get("ip"),
                    "link_type": "Wire",
                    "band": None,
                    "ssid": None,
                    "rssi": None,
                    "rate": entry.get("rate"),
                    "connected_via": "LAN",
                }
            )
        # Any other/unknown type is intentionally ignored rather than
        # guessed at.

    return nodes, clients


def _process_ethernet_ports(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn dashboard.interface's eth0_status/eth0_speed... pairs into a list.

    Note: we don't yet know which ethX index is the physical WAN port vs a
    LAN port on this specific router -- that would need to be confirmed
    against the front-panel labelling, so we just expose them by their raw
    interface name for now.
    """
    interface = dashboard.get("interface", {})
    ports: list[dict[str, Any]] = []
    index = 0
    while f"eth{index}_status" in interface:
        ports.append(
            {
                "port": f"eth{index}",
                "connected": interface.get(f"eth{index}_status") == "yes",
                "speed": interface.get(f"eth{index}_speed"),
            }
        )
        index += 1
    return ports
