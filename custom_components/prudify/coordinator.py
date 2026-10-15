"""Polls Prudify, and turns changes between polls into events."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PrudifyAuthError, PrudifyClient, PrudifyError
from .const import (
    DOMAIN,
    EVENT_JOB_FINISHED,
    EVENT_QUEUE_HELD,
    EVENT_QUEUE_RESUMED,
    EVENT_QUEUE_SUSPENDED,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type PrudifyConfigEntry = ConfigEntry[PrudifyCoordinator]


class PrudifyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    config_entry: PrudifyConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: PrudifyConfigEntry, client: PrudifyClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.client.summary()
        except PrudifyAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except PrudifyError as exc:
            raise UpdateFailed(str(exc)) from exc
        if self.data is not None:
            self._fire_changes(self.data, data)
        return data

    def _fire_changes(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        """Events for what happened between two polls.

        Polling every 30 seconds can miss a book that finished and another
        that finished straight after; it reports the latest, which is the one
        a notification should be about anyway.
        """
        bus = self.hass.bus
        finished = after.get("last_finished") or {}
        previous = before.get("last_finished") or {}
        if finished and finished.get("job_id") != previous.get("job_id"):
            bus.async_fire(EVENT_JOB_FINISHED, finished)

        if after.get("paused_reason") and not before.get("paused_reason"):
            bus.async_fire(EVENT_QUEUE_SUSPENDED, {"reason": after["paused_reason"]})
        elif before.get("paused_reason") and not after.get("paused_reason"):
            bus.async_fire(EVENT_QUEUE_RESUMED, {})

        if after.get("held_reason") != before.get("held_reason"):
            bus.async_fire(EVENT_QUEUE_HELD, {"reason": after.get("held_reason") or ""})
