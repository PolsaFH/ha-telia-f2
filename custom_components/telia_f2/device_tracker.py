"""Device tracker platform: one presence entity per client the router sees.

Each connected client (wired or wireless) becomes a `device_tracker` that is
"home" while the router lists it, and stays home for CONSIDER_HOME_SECONDS
after it drops off, so a phone that briefly sleeps its Wi-Fi radio doesn't
flap to "away". Trackers are created as new MACs appear and are kept (so they
report "away") once a device leaves.
"""
from __future__ import annotations

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TeliaF2Coordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TeliaF2Coordinator = hass.data[DOMAIN][entry.entry_id]
    tracked: set[str] = set()

    @callback
    def _add_new_trackers() -> None:
        new = [
            TeliaF2DeviceTracker(coordinator, mac)
            for mac in coordinator.seen_macs()
            if mac and mac not in tracked
        ]
        for entity in new:
            tracked.add(entity.mac_address)
        if new:
            async_add_entities(new)

    _add_new_trackers()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_trackers))


class TeliaF2DeviceTracker(CoordinatorEntity[TeliaF2Coordinator], ScannerEntity):
    """Presence of a single client, keyed by its MAC address."""

    _attr_source_type = SourceType.ROUTER

    def __init__(self, coordinator: TeliaF2Coordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        # ScannerEntity uses mac_address as the unique_id.
        self._attr_mac_address = mac

    @property
    def is_connected(self) -> bool:
        return self.coordinator.is_home(self._mac)

    @property
    def ip_address(self) -> str | None:
        record = self.coordinator.client_record(self._mac)
        return record.get("ip") if record else None

    @property
    def hostname(self) -> str | None:
        record = self.coordinator.client_record(self._mac)
        return record.get("hostname") if record else None

    @property
    def name(self) -> str:
        record = self.coordinator.client_record(self._mac)
        hostname = record.get("hostname") if record else None
        return hostname or self._mac
