"""Coordinator that polls the Telia F2 router and normalizes the data.

/status/device returns a flat "list" that mixes two kinds of entries:
  - Mesh nodes (type "Controller" or "Extender"): these have their own
    "radios" -> "stations" nesting, listing the WIRELESS clients connected
    to THAT specific node. The node entry itself also carries top-level
    band/rssi/noise/rate fields for an Extender, which is that extender's
    own backhaul link quality back to the Controller.
  - Wired clients (type "LAN"): flat entries with hostname/mac/ip/rate
    directly, not nested under anything.

/status/statistics returns one "statistics" string: the raw /proc/net/dev
table (per-interface byte/packet counters). /status/wan returns the WAN
interface state (protocol, uptime, addresses, gateway, DNS). Both are
optional: a firmware that lacks them must not break the whole update.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TeliaF2ApiError, TeliaF2AuthError, TeliaF2Client
from .const import CONSIDER_HOME_SECONDS, DOMAIN, UPDATE_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

NODE_TYPES = {"Controller", "Extender"}

# Fallback WAN interface name when /status/wan doesn't name its device. On
# this hardware the internet-facing bridge is br-wan (rx = download from the
# internet, tx = upload to it).
DEFAULT_WAN_DEVICE = "br-wan"

# The router rejects rapid back-to-back SSID writes, so pace them.
WIFI_WRITE_PACING_SECONDS = 0.4


class TeliaF2Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the router and merges the responses into one data dict."""

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
        # Previous WAN byte counters as (monotonic_time, device, rx, tx), so
        # a rate is only derived from two readings of the SAME interface.
        self._prev_wan: tuple[float, str, int, int] | None = None
        # mac -> monotonic time the client was last seen, for device trackers.
        self._seen: dict[str, float] = {}
        # Serialises every Wi-Fi write transaction against this router, so two
        # switches can never commit each other's half-applied changes.
        self._wifi_lock = asyncio.Lock()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            dashboard = await self.client.async_get_dashboard()
            status_device = await self.client.async_get_status_device()
            # Optional endpoints: a non-auth failure degrades to the core
            # sensors (see _optional), but an auth failure must still reach
            # the handler below so polling stops and reauth starts.
            statistics = await self._optional(self.client.async_get_status_statistics)
            wan = await self._optional(self.client.async_get_status_wan)
            ssids = await self._optional(self.client.async_get_airties_ssids)
        except TeliaF2AuthError as err:
            # Stops polling and asks for the password right away: repeating a
            # rejected login could make the router block further logins.
            raise ConfigEntryAuthFailed(str(err)) from err
        except TeliaF2ApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

        nodes, clients = _process_status_device(status_device)
        ports = _process_ethernet_ports(dashboard)
        interfaces = _parse_proc_net_dev(
            statistics.get("statistics") if isinstance(statistics, dict) else None
        )
        throughput = self._compute_throughput(interfaces, wan)

        now = time.monotonic()
        for client in clients:
            mac = client.get("mac")
            if mac:
                self._seen[mac] = now

        return {
            "dashboard": dashboard,
            "nodes": nodes,
            "clients": clients,
            "ports": ports,
            "interfaces": interfaces,
            "throughput": throughput,
            "wan": wan if isinstance(wan, dict) else {},
            "wifi_ssids": (ssids or {}).get("list", []) if isinstance(ssids, dict) else [],
        }

    async def _optional(
        self, call: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any] | None:
        """Call an endpoint that may not exist.

        A non-auth API error (missing endpoint, connection blip, rate limit)
        degrades to None so the core sensors keep working. TeliaF2AuthError
        is deliberately NOT caught here: it must propagate so the caller can
        turn it into a reauth, exactly as for the required endpoints.
        """
        try:
            return await call()
        except TeliaF2AuthError:
            raise
        except TeliaF2ApiError as err:
            _LOGGER.debug("Optional endpoint unavailable: %s", err)
            return None

    def _compute_throughput(
        self, interfaces: dict[str, dict[str, int]], wan: Any
    ) -> dict[str, Any]:
        """Turn the WAN interface's byte counters into totals and a live rate."""
        device = DEFAULT_WAN_DEVICE
        if isinstance(wan, dict):
            # Only a non-empty string is a usable interface name; the router
            # normally sends one, but guard against odd/corrupt shapes.
            for candidate in (wan.get("l3_device"), wan.get("device")):
                if isinstance(candidate, str) and candidate:
                    device = candidate
                    break

        counters = interfaces.get(device)
        if not counters:
            self._prev_wan = None
            return {
                "device": device,
                "rx_bytes": None,
                "tx_bytes": None,
                "rx_mbps": None,
                "tx_mbps": None,
            }

        rx, tx = counters["rx_bytes"], counters["tx_bytes"]
        now = time.monotonic()
        rx_mbps = tx_mbps = None
        if self._prev_wan is not None:
            prev_t, prev_dev, prev_rx, prev_tx = self._prev_wan
            dt = now - prev_t
            # A rate only makes sense between two readings of the same
            # interface over a real interval with forward-moving counters.
            # A reboot (counter reset) or an interface change would otherwise
            # read as a huge spike.
            if prev_dev == device and dt > 0 and rx >= prev_rx and tx >= prev_tx:
                rx_mbps = round((rx - prev_rx) * 8 / dt / 1_000_000, 3)
                tx_mbps = round((tx - prev_tx) * 8 / dt / 1_000_000, 3)
        self._prev_wan = (now, device, rx, tx)

        return {
            "device": device,
            "rx_bytes": rx,
            "tx_bytes": tx,
            "rx_mbps": rx_mbps,
            "tx_mbps": tx_mbps,
        }

    def seen_macs(self) -> list[str]:
        """Every client MAC seen since startup (a tracker is kept per MAC)."""
        return list(self._seen)

    def is_home(self, mac: str) -> bool:
        """Whether a MAC was seen recently enough to count as home."""
        last_seen = self._seen.get(mac)
        if last_seen is None:
            return False
        return (time.monotonic() - last_seen) <= CONSIDER_HOME_SECONDS

    def client_record(self, mac: str) -> dict[str, Any] | None:
        """The current client entry for a MAC, if it is connected right now."""
        for client in self.data.get("clients", []):
            if client.get("mac") == mac:
                return client
        return None

    async def async_set_wifi_enabled(
        self, wifi_type: str, band: str | None, value: str
    ) -> None:
        """Turn a Wi-Fi network/band on or off, safely.

        The whole transaction runs under one lock: a FRESH read of the SSID
        entries (never the poll cache, so a password/SSID changed in the
        router UI since the last poll is not written back), then a
        read-modify-write that only flips ``enable`` to "1"/"0" on the
        affected entries, then a single commit. Writes are paced because the
        router rejects rapid bursts. If any write fails the commit is skipped
        and the error propagates, so a partial change is never committed here.
        """
        async with self._wifi_lock:
            data = await self.client.async_get_airties_ssids()
            entries = [
                e
                for e in data.get("list", [])
                if e.get("type") == wifi_type
                and (band is None or e.get("band") == band)
            ]
            if not entries:
                raise TeliaF2ApiError(f"No {wifi_type} Wi-Fi network to change")

            changed = False
            for entry in entries:
                if str(entry.get("enable")) == value:
                    continue
                if changed:
                    await asyncio.sleep(WIFI_WRITE_PACING_SECONDS)
                payload = dict(entry)
                payload["enable"] = value
                await self.client.async_set_airties_ssid(payload)
                changed = True
            if changed:
                await self.client.async_commit_airties()
            await self.async_request_refresh()


def _parse_proc_net_dev(text: Any) -> dict[str, dict[str, int]]:
    """Parse the /proc/net/dev table into {interface: {rx_bytes, tx_bytes}}.

    The two header lines are skipped. Each data line is
    "iface: rxbytes rxpackets ... (16 columns)"; column 0 is rx bytes and
    column 8 is tx bytes. Anything that isn't a well-formed data line is
    ignored rather than guessed at.
    """
    result: dict[str, dict[str, int]] = {}
    if not isinstance(text, str):
        return result
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if not name or len(fields) < 16:
            continue
        try:
            result[name] = {"rx_bytes": int(fields[0]), "tx_bytes": int(fields[8])}
        except ValueError:
            continue
    return result


def _process_status_device(
    status_device: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Split /status/device's flat list into mesh nodes and end-clients."""
    nodes: dict[str, dict[str, Any]] = {}
    clients: list[dict[str, Any]] = []

    for entry in status_device.get("list", []):
        entry_type = entry.get("type")

        if entry_type in NODE_TYPES:
            node_key = entry.get("mac") or entry.get("hostname")
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
