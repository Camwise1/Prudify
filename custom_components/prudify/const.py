"""Constants for the Prudify integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "prudify"
DEFAULT_PORT = 8317
UPDATE_INTERVAL = timedelta(seconds=30)

# Fired on the Home Assistant event bus, for automations and notifications.
EVENT_JOB_FINISHED = f"{DOMAIN}_job_finished"
EVENT_QUEUE_SUSPENDED = f"{DOMAIN}_queue_suspended"
EVENT_QUEUE_RESUMED = f"{DOMAIN}_queue_resumed"
EVENT_QUEUE_HELD = f"{DOMAIN}_queue_held"

STATES = ["processing", "idle", "paused", "suspended", "holding"]
