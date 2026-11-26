"""Switch platform: turn Wi-Fi bands / the guest & IoT networks on and off.

Each Wi-Fi network is stored as one SSID entry per band in
`/airties/ssid/all`, with an `enable` flag. Writing a band on/off is a
read-modify-write of that whole entry (its name, ssid, key, ... kept) with
`enable` set to "1"/"0", followed by a commit. A per-band switch targets one
home entry; the guest/IoT switches target every band of that network at once.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TeliaF2Coordinator

_LOGGER = logging.getLogger(__name__)

# Band key in the router data -> label shown in Home Assistant.
BAND_LABELS = {"2g": "2.4 GHz", "5g": "5 GHz", "6g": "6 GHz"}
# Order the band switches predictably.
BAND_ORDER = ["2g", "5g", "6g"]


def _enabled(entry: dict[str, Any]) -> bool:
    return str(entry.get("enable")) == "1"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TeliaF2Coordinator = hass.data[DOMAIN][entry.entry_id]
    ssids = coordinator.data.get("wifi_ssids", [])

    entities: list[SwitchEntity] = []
    # Per-band switches for the main ("home") network.
    home_bands = {s.get("band") for s in ssids if s.get("type") == "home"}
    for band in BAND_ORDER:
        if band in home_bands:
            entities.append(
                TeliaF2WifiSwitch(
                    coordinator, entry, "home", band,
                    name=f"Wi-Fi {BAND_LABELS.get(band, band)}",
                    key=f"wifi_home_{band}",
                    icon="mdi:wifi",
                )
            )
    # One switch each for the guest and IoT networks (all their bands at once).
    if any(s.get("type") == "guest" for s in ssids):
        entities.append(
            TeliaF2WifiSwitch(
                coordinator, entry, "guest", None,
                name="Guest Wi-Fi", key="wifi_guest", icon="mdi:wifi-lock",
            )
        )
    if any(s.get("type") == "iot" for s in ssids):
        entities.append(
            TeliaF2WifiSwitch(
                coordinator, entry, "iot", None,
                name="IoT Wi-Fi", key="wifi_iot", icon="mdi:wifi-cog",
            )
        )
    async_add_entities(entities)


class TeliaF2WifiSwitch(CoordinatorEntity[TeliaF2Coordinator], SwitchEntity):
    """On/off for one home band, or for a whole guest/IoT network."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator: TeliaF2Coordinator,
        entry: ConfigEntry,
        wifi_type: str,
        band: str | None,
        *,
        name: str,
        key: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._type = wifi_type
        self._band = band
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    def _entries(self) -> list[dict[str, Any]]:
        """The SSID entries this switch controls."""
        return [
            s
            for s in self.coordinator.data.get("wifi_ssids", [])
            if s.get("type") == self._type
            and (self._band is None or s.get("band") == self._band)
        ]

    @property
    def available(self) -> bool:
        return super().available and bool(self._entries())

    @property
    def is_on(self) -> bool | None:
        entries = self._entries()
        if not entries:
            return None
        # A group (guest/IoT) counts as on if any of its bands is on.
        return any(_enabled(e) for e in entries)

    async def _async_apply(self, value: str) -> None:
        entries = self._entries()
        if not entries:
            return
        changed = False
        for entry in entries:
            if str(entry.get("enable")) == value:
                continue  # already in the wanted state
            if changed:
                # The router rejects rapid back-to-back writes, so pace them.
                await asyncio.sleep(0.4)
            payload = dict(entry)
            payload["enable"] = value
            await self.coordinator.client.async_set_airties_ssid(payload)
            changed = True
        if changed:
            await self.coordinator.client.async_commit_airties()
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_apply("1")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_apply("0")
