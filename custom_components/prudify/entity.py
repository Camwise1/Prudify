"""What every Prudify entity shares: its device and its coordinator."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PrudifyCoordinator


class PrudifyEntity(CoordinatorEntity[PrudifyCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PrudifyCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Prudify",
            manufacturer="Camwise",
            model="Audiobook profanity filter",
            sw_version=(coordinator.data or {}).get("version"),
            configuration_url=coordinator.client.url,
            entry_type=DeviceEntryType.SERVICE,
        )
