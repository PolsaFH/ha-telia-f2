"""Binary sensor platform for the Telia F2 router integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TeliaF2Coordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TeliaF2Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TeliaF2WanConnected(coordinator, entry),
            TeliaF2ExtenderConnected(coordinator, entry),
        ]
    )


class TeliaF2WanConnected(CoordinatorEntity[TeliaF2Coordinator], BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "WAN connected"

    def __init__(self, coordinator: TeliaF2Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_connected"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def is_on(self) -> bool | None:
        dashboard = self.coordinator.data.get("dashboard", {})
        wan = dashboard.get("wan_status", {}).get("wan", {})
        return wan.get("up")


class TeliaF2ExtenderConnected(
    CoordinatorEntity[TeliaF2Coordinator], BinarySensorEntity
):
    """Whether the mesh extender currently shows up in /status/device at
    all. Its absence from that list means it's lost its backhaul link to
    the Controller. When present, its own backhaul signal quality is
    exposed as attributes (rssi/band/rate back to the Controller)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Extender connected"

    def __init__(self, coordinator: TeliaF2Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_extender_connected"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    def _extender_node(self) -> dict[str, Any] | None:
        for node in self.coordinator.data.get("nodes", {}).values():
            if node.get("type") == "Extender":
                return node
        return None

    @property
    def is_on(self) -> bool:
        return self._extender_node() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        node = self._extender_node()
        if not node:
            return {}
        return {
            "hostname": node.get("hostname"),
            "ip": node.get("ip"),
            "mac": node.get("mac"),
            "firmware_version": node.get("firmware_version"),
            "backhaul_band": node.get("backhaul_band"),
            "backhaul_rssi": node.get("backhaul_rssi"),
            "backhaul_rate": node.get("backhaul_rate"),
        }
