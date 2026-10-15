"""The Prudify integration, against a mocked Prudify server."""

from __future__ import annotations

import copy

import pytest
from custom_components.prudify.const import (
    DOMAIN,
    EVENT_JOB_FINISHED,
    EVENT_QUEUE_RESUMED,
    EVENT_QUEUE_SUSPENDED,
)
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

URL = "http://prudify.test:8317"
SUMMARY = {
    "version": "2.1.0",
    "state": "processing",
    "paused": False,
    "paused_reason": "",
    "held_reason": "",
    "active": {
        "job_id": 40,
        "title": "Ship of Magic",
        "author": "Robin Hobb",
        "mode": "clean",
        "stage": "transcribing",
        "message": "transcribing chunk 64/71",
        "progress": 64.1,
        "speed": 7.3,
        "finishes_at": "2026-10-15T08:00:00+00:00",
        "finish_is_estimate_of": "job",
    },
    "pending": 10,
    "pending_audio_seconds": 464729.0,
    "queue_finishes_at": "2026-10-16T04:00:00+00:00",
    "books": 346,
    "cleaned": 30,
    "failed": 1,
    "waiting": 300,
    "instances_silenced": 1234,
    "muted_seconds": 600.0,
    "last_finished": {
        "job_id": 39,
        "title": "The Bourne Identity",
        "author": "Robert Ludlum",
        "status": "completed",
        "matches": 87,
        "muted_seconds": 41.5,
        "mode": "clean",
        "finished_at": "2026-10-15T03:00:00+00:00",
    },
}


def _serve(aioclient_mock: AiohttpClientMocker, summary=SUMMARY, status=200) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/v1/system/summary", json=summary, status=status)
    for action in ("queue/pause", "queue/resume", "books/scan"):
        aioclient_mock.post(f"{URL}/api/v1/{action}", json={})


async def _setup(hass: HomeAssistant, aioclient_mock, summary=SUMMARY) -> MockConfigEntry:
    _serve(aioclient_mock, summary)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: URL, CONF_API_KEY: "key"}, unique_id=URL
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# -- config flow ------------------------------------------------------------


async def test_config_flow_creates_an_entry(hass: HomeAssistant, aioclient_mock) -> None:
    _serve(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL + "/", CONF_API_KEY: "key"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL, CONF_API_KEY: "key"}


@pytest.mark.parametrize(("status", "error"), [(401, "invalid_auth"), (500, "cannot_connect")])
async def test_config_flow_explains_a_failure(
    hass: HomeAssistant, aioclient_mock, status, error
) -> None:
    _serve(aioclient_mock, status=status)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API_KEY: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


# -- entities ---------------------------------------------------------------


async def test_sensors_show_what_prudify_is_doing(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)

    assert hass.states.get("sensor.prudify_status").state == "processing"
    book = hass.states.get("sensor.prudify_current_book")
    assert book.state == "Ship of Magic"
    assert book.attributes["author"] == "Robin Hobb"
    assert book.attributes["step"] == "transcribing chunk 64/71"
    assert float(hass.states.get("sensor.prudify_progress").state) == pytest.approx(64.1)
    assert hass.states.get("sensor.prudify_current_book_finishes").state == (
        "2026-10-15T08:00:00+00:00"
    )
    queue = hass.states.get("sensor.prudify_books_waiting")
    assert queue.state == "10"
    assert queue.attributes["audio_hours"] == 129.1
    assert hass.states.get("sensor.prudify_instances_silenced").state == "1234"
    assert hass.states.get("sensor.prudify_last_finished").attributes["matches"] == 87
    assert hass.states.get("binary_sensor.prudify_library_unreachable").state == "off"
    assert hass.states.get("switch.prudify_queue").state == "on"


async def test_nothing_running_reads_as_unknown_not_stale(
    hass: HomeAssistant, aioclient_mock
) -> None:
    idle = {**SUMMARY, "state": "idle", "active": None}
    await _setup(hass, aioclient_mock, idle)
    assert hass.states.get("sensor.prudify_current_book").state == "unknown"
    assert hass.states.get("sensor.prudify_progress").state == "unknown"


async def test_the_switch_pauses_and_resumes_the_queue(
    hass: HomeAssistant, aioclient_mock
) -> None:
    await _setup(hass, aioclient_mock)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.prudify_queue"}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.prudify_queue"}, blocking=True
    )
    posted = [str(call[1]) for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posted == [f"{URL}/api/v1/queue/pause", f"{URL}/api/v1/queue/resume"]


async def test_the_button_scans_the_library(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.prudify_scan_library"}, blocking=True
    )
    assert any(
        call[0] == "POST" and str(call[1]).endswith("/books/scan")
        for call in aioclient_mock.mock_calls
    )


# -- events, for notifications ---------------------------------------------


async def test_events_report_what_changed_between_polls(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = await _setup(hass, aioclient_mock)
    finished = async_capture_events(hass, EVENT_JOB_FINISHED)
    suspended = async_capture_events(hass, EVENT_QUEUE_SUSPENDED)
    resumed = async_capture_events(hass, EVENT_QUEUE_RESUMED)

    later = copy.deepcopy(SUMMARY)
    later["last_finished"] = {**later["last_finished"], "job_id": 40, "title": "Ship of Magic"}
    later["paused_reason"] = "Library Audiobooks is unreachable (timed out)"
    _serve(aioclient_mock, later)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert [event.data["title"] for event in finished] == ["Ship of Magic"]
    assert suspended[0].data["reason"].startswith("Library Audiobooks")
    assert hass.states.get("binary_sensor.prudify_library_unreachable").state == "on"

    _serve(aioclient_mock, {**later, "paused_reason": ""})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(resumed) == 1
    assert len(finished) == 1  # the same book is not announced twice


async def test_a_rejected_key_asks_for_a_new_one(hass: HomeAssistant, aioclient_mock) -> None:
    _serve(aioclient_mock, status=401)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_URL: URL, CONF_API_KEY: "old"}, unique_id=URL
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]

    _serve(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {CONF_API_KEY: "new"}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new"
