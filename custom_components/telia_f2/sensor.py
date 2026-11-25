"""Sensor platform for the Telia F2 router integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TeliaF2Coordinator


def _get(data: dict[str, Any], *path: str, default: Any = None) -> Any:
    """Safely walk a nested dict by a sequence of keys."""
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _wan_gateway(data: dict[str, Any]) -> Any:
    """Return the WAN default-route next hop (the gateway), if present."""
    for route in _get(data, "wan", "route", default=[]) or []:
        if isinstance(route, dict) and route.get("mask") == 0:
            return route.get("nexthop")
    return None


def _count_clients(data: dict[str, Any], link_type: str) -> int:
    return sum(
        1 for c in data.get("clients", []) if c.get("link_type") == link_type
    )


@dataclass(frozen=True, kw_only=True)
class TeliaF2SensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any] = lambda data: None


SIMPLE_SENSOR_DESCRIPTIONS: tuple[TeliaF2SensorDescription, ...] = (
    TeliaF2SensorDescription(
        key="cpu_load",
        translation_key="cpu_load",
        name="CPU load",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _get(d, "dashboard", "cpu", "all"),
    ),
    TeliaF2SensorDescription(
        key="memory_free",
        translation_key="memory_free",
        name="Free memory",
        native_unit_of_measurement="kB",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _get(d, "dashboard", "mem", "free"),
    ),
    TeliaF2SensorDescription(
        key="memory_used",
        translation_key="memory_used",
        name="Memory usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _get(d, "dashboard", "mem", "used"),
    ),
    TeliaF2SensorDescription(
        key="uptime",
        translation_key="uptime",
        name="Uptime",
        value_fn=lambda d: _get(d, "dashboard", "uptime", "uptime"),
    ),
    TeliaF2SensorDescription(
        key="wan_ip",
        translation_key="wan_ip",
        name="WAN IP address",
        value_fn=lambda d: (
            _get(d, "dashboard", "wan_status", "wan", "ipv4-address", default=[{}])
            or [{}]
        )[0].get("address"),
    ),
    # --- WAN connection details (/status/wan) ---
    TeliaF2SensorDescription(
        key="wan_uptime",
        translation_key="wan_uptime",
        name="WAN uptime",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: _get(d, "wan", "uptime"),
    ),
    TeliaF2SensorDescription(
        key="wan_gateway",
        translation_key="wan_gateway",
        name="WAN gateway",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_wan_gateway,
    ),
    # --- Throughput / data usage (/status/statistics, WAN interface) ---
    TeliaF2SensorDescription(
        key="download_speed",
        translation_key="download_speed",
        name="Download speed",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: _get(d, "throughput", "rx_mbps"),
    ),
    TeliaF2SensorDescription(
        key="upload_speed",
        translation_key="upload_speed",
        name="Upload speed",
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: _get(d, "throughput", "tx_mbps"),
    ),
    TeliaF2SensorDescription(
        key="wan_data_received",
        translation_key="wan_data_received",
        name="WAN data received",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _get(d, "throughput", "rx_bytes"),
    ),
    TeliaF2SensorDescription(
        key="wan_data_sent",
        translation_key="wan_data_sent",
        name="WAN data sent",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _get(d, "throughput", "tx_bytes"),
    ),
    # --- Client counts ---
    TeliaF2SensorDescription(
        key="wireless_clients",
        translation_key="wireless_clients",
        name="Wireless clients",
        native_unit_of_measurement="clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _count_clients(d, "Wi-Fi"),
    ),
    TeliaF2SensorDescription(
        key="wired_clients",
        translation_key="wired_clients",
        name="Wired clients",
        native_unit_of_measurement="clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: _count_clients(d, "Wire"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TeliaF2Coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        TeliaF2Sensor(coordinator, entry, description)
        for description in SIMPLE_SENSOR_DESCRIPTIONS
    ]
    entities.append(TeliaF2DeviceListSensor(coordinator, entry))
    entities.append(TeliaF2EthernetPortsSensor(coordinator, entry))
    async_add_entities(entities)


def _device_info(coordinator: TeliaF2Coordinator, entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=_get(coordinator.data, "dashboard", "info", "friendly_name")
        or "Telia F2",
        manufacturer=_get(coordinator.data, "dashboard", "info", "manufacturer"),
        model=_get(coordinator.data, "dashboard", "info", "model_name"),
        sw_version=_get(coordinator.data, "dashboard", "info", "software_version"),
    )


class TeliaF2Sensor(CoordinatorEntity[TeliaF2Coordinator], SensorEntity):
    entity_description: TeliaF2SensorDescription

    def __init__(
        self,
        coordinator: TeliaF2Coordinator,
        entry: ConfigEntry,
        description: TeliaF2SensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)


class TeliaF2DeviceListSensor(CoordinatorEntity[TeliaF2Coordinator], SensorEntity):
    """Single entity listing every wired + wireless client currently seen,
    which node/radio it's connected via, and its signal/link details --
    instead of one HA entity per client."""

    _attr_name = "Connected devices"
    _attr_icon = "mdi:devices"

    def __init__(self, coordinator: TeliaF2Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_device_list"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.get("clients", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        clients = self.coordinator.data.get("clients", [])
        nodes = self.coordinator.data.get("nodes", {})
        wireless = [c for c in clients if c.get("link_type") == "Wi-Fi"]
        wired = [c for c in clients if c.get("link_type") == "Wire"]
        return {
            "devices": clients,
            "wireless_count": len(wireless),
            "wired_count": len(wired),
            "nodes": list(nodes.values()),
        }


class TeliaF2EthernetPortsSensor(CoordinatorEntity[TeliaF2Coordinator], SensorEntity):
    """Single entity listing all ethernet ports and their link status,
    instead of one entity per port."""

    _attr_name = "Ethernet ports"
    _attr_icon = "mdi:ethernet"

    def __init__(self, coordinator: TeliaF2Coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ethernet_ports"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> str:
        ports = self.coordinator.data.get("ports", [])
        connected = sum(1 for p in ports if p.get("connected"))
        return f"{connected}/{len(ports)}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"ports": self.coordinator.data.get("ports", [])}
