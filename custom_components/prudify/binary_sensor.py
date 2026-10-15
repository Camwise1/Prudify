"""On when Prudify has paused itself because a library cannot be reached."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrudifyConfigEntry
from .entity import PrudifyEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrudifyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PrudifyLibraryProblem(entry.runtime_data)])


class PrudifyLibraryProblem(PrudifyEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "library_problem")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("paused_reason"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"reason": self.coordinator.data.get("paused_reason") or None}
