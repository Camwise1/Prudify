"""The queue as a switch: on while it is allowed to run."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([PrudifyQueueSwitch(entry.runtime_data)])


class PrudifyQueueSwitch(PrudifyEntity, SwitchEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "queue_running")

    @property
    def is_on(self) -> bool:
        return not self.coordinator.data.get("paused")

    async def _act(self, call) -> None:
        try:
            await call()
        except PrudifyError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._act(self.coordinator.client.resume)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._act(self.coordinator.client.pause)
