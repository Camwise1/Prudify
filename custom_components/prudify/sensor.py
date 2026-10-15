"""Sensors: what Prudify is doing, how long it will take, and what it has done."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import STATES
from .coordinator import PrudifyConfigEntry
from .entity import PrudifyEntity


def _active(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("active") or {}


def _time(value: str | None) -> datetime | None:
    return dt_util.parse_datetime(value) if value else None


@dataclass(frozen=True, kw_only=True)
class PrudifySensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]
    attributes: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[PrudifySensorDescription, ...] = (
    PrudifySensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATES,
        value=lambda d: d.get("state"),
        attributes=lambda d: {
            "paused_reason": d.get("paused_reason") or None,
            "held_reason": d.get("held_reason") or None,
        },
    ),
    PrudifySensorDescription(
        key="current_book",
        value=lambda d: _active(d).get("title"),
        attributes=lambda d: {
            "author": _active(d).get("author"),
            "stage": _active(d).get("stage"),
            "step": _active(d).get("message"),
            "copy_only": _active(d).get("mode") == "copy" if _active(d) else None,
        },
    ),
    PrudifySensorDescription(
        key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda d: _active(d).get("progress"),
    ),
    PrudifySensorDescription(
        key="book_finishes",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: _time(_active(d).get("finishes_at")),
        attributes=lambda d: {"estimate_covers": _active(d).get("finish_is_estimate_of")},
    ),
    PrudifySensorDescription(
        key="speed",
        native_unit_of_measurement="×",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda d: _active(d).get("speed"),
    ),
    PrudifySensorDescription(
        key="queue",
        native_unit_of_measurement="books",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda d: d.get("pending"),
        attributes=lambda d: {
            "audio_hours": round((d.get("pending_audio_seconds") or 0) / 3600, 1),
        },
    ),
    PrudifySensorDescription(
        key="queue_finishes",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: _time(d.get("queue_finishes_at")),
    ),
    PrudifySensorDescription(
        key="books_cleaned",
        native_unit_of_measurement="books",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda d: d.get("cleaned"),
    ),
    PrudifySensorDescription(
        key="books_failed",
        native_unit_of_measurement="books",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda d: d.get("failed"),
    ),
    PrudifySensorDescription(
        key="books_waiting",
        native_unit_of_measurement="books",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda d: d.get("waiting"),
    ),
    PrudifySensorDescription(
        key="instances_silenced",
        state_class=SensorStateClass.TOTAL,
        value=lambda d: d.get("instances_silenced"),
    ),
    PrudifySensorDescription(
        key="audio_muted",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value=lambda d: d.get("muted_seconds"),
    ),
    PrudifySensorDescription(
        key="last_finished",
        value=lambda d: (d.get("last_finished") or {}).get("title"),
        attributes=lambda d: {
            key: (d.get("last_finished") or {}).get(key)
            for key in ("author", "status", "matches", "muted_seconds", "mode", "finished_at")
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrudifyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(PrudifySensor(entry.runtime_data, description) for description in SENSORS)


class PrudifySensor(PrudifyEntity, SensorEntity):
    entity_description: PrudifySensorDescription

    def __init__(self, coordinator, description: PrudifySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        value = self.entity_description.value(self.coordinator.data)
        # Titles can exceed the 255 characters a state may hold.
        if isinstance(value, str):
            return value[:255]
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator.data)
