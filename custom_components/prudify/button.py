"""Ask Prudify to look for new books now rather than at its next scheduled scan."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PrudifyError
from .coordinator import PrudifyConfigEntry
from .entity import PrudifyEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrudifyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PrudifyScanButton(entry.runtime_data)])


class PrudifyScanButton(PrudifyEntity, ButtonEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "scan_library")

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.scan()
        except PrudifyError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await self.coordinator.async_request_refresh()
