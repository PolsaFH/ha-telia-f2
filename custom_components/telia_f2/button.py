"""Button platform for the Telia F2 router integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import TeliaF2Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TeliaF2Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TeliaF2RestartButton(coordinator, entry)])


class TeliaF2RestartButton(ButtonEntity):
    """Reboots the router via POST /admin/reboot (confirmed working, no
    encrypted payload needed -- msg is empty)."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_name = "Restart"

    def __init__(self, coordinator: TeliaF2Coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_restart"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self) -> None:
        _LOGGER.info("Telia F2: restarting router")
        await self._coordinator.client.async_call("/admin/reboot", action="post")
