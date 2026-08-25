"""HTTP API behaviour, exercised through FastAPI's TestClient."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prudify.config import LibrarySettings, save_config
from prudify.main import create_app


@pytest.fixture
def app_client(config, tmp_path):
    source = tmp_path / "audiobooks"
    (source / "Author/Book").mkdir(parents=True)
    (source / "Author/Book/Book.m4b").write_bytes(b"\0" * 4096)
    config.libraries = [
        LibrarySettings(
            name="Test",
            source_path=str(source),
            output_path=str(tmp_path / "clean"),
            auto_process=False,
        )
    ]
    config.processing.scan_interval_minutes = 0
    save_config(config)

    app = create_app(config)
    with TestClient(app) as client:
        client.headers.update({"X-Api-Key": config.server.api_key})
        # Hold the workers so queue assertions are not racing a live job.
        client.post("/api/v1/queue/pause")
        yield client, config


def test_ping_needs_no_key(app_client):
    client, _ = app_client
    response = client.get("/ping", headers={"X-Api-Key": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_requires_a_key(app_client):
    client, _ = app_client
    assert client.get("/api/v1/system/status", headers={"X-Api-Key": "wrong"}).status_code == 401


def test_status_reports_environment(app_client):
    client, _ = app_client
    body = client.get("/api/v1/system/status").json()
    assert body["libraries"] == 1
    assert "stats" in body


def test_scan_then_list_books(app_client):
    client, _ = app_client
    client.post("/api/v1/books/scan")
    page = client.get("/api/v1/books").json()
    assert page["total"] == 1
    assert page["items"][0]["title"] == "Book"
    assert page["items"][0]["author"] == "Author"


def test_book_detail_includes_parts(app_client):
    client, _ = app_client
    client.post("/api/v1/books/scan")
    book_id = client.get("/api/v1/books").json()["items"][0]["id"]
    detail = client.get(f"/api/v1/books/{book_id}").json()
    assert len(detail["parts"]) == 1
    assert detail["parts"][0]["relative_path"] == "Author/Book/Book.m4b"


def test_unknown_book_is_404(app_client):
    client, _ = app_client
    assert client.get("/api/v1/books/does-not-exist").status_code == 404


def test_queue_a_book(app_client):
    client, _ = app_client
    client.post("/api/v1/books/scan")
    book_id = client.get("/api/v1/books").json()["items"][0]["id"]

    assert client.post(f"/api/v1/books/{book_id}/queue").json()["queued"] is True
    state = client.get("/api/v1/queue").json()
    assert len(state["pending"]) + len(state["active"]) == 1


def test_queueing_twice_does_not_duplicate(app_client):
    client, _ = app_client
    client.post("/api/v1/books/scan")
    book_id = client.get("/api/v1/books").json()["items"][0]["id"]
    first = client.post(f"/api/v1/books/{book_id}/queue").json()["job_id"]
    second = client.post(f"/api/v1/books/{book_id}/queue").json()["job_id"]
    assert first == second


def test_pause_and_resume(app_client):
    client, _ = app_client
    assert client.post("/api/v1/queue/pause").json()["paused"] is True
    assert client.get("/api/v1/queue").json()["paused"] is True
    assert client.post("/api/v1/queue/resume").json()["paused"] is False


def test_settings_round_trip(app_client):
    client, _ = app_client
    settings = client.get("/api/v1/settings").json()
    settings["filtering"]["pad_after_ms"] = 275

    updated = client.put(
        "/api/v1/settings", json={"filtering": settings["filtering"]}
    ).json()
    assert updated["filtering"]["pad_after_ms"] == 275
    assert client.get("/api/v1/settings").json()["filtering"]["pad_after_ms"] == 275


def test_settings_validation_rejects_bad_values(app_client):
    client, _ = app_client
    settings = client.get("/api/v1/settings").json()
    settings["filtering"]["pad_after_ms"] = -5
    response = client.put("/api/v1/settings", json={"filtering": settings["filtering"]})
    assert response.status_code == 422


def test_library_crud(app_client, tmp_path):
    client, _ = app_client
    source = tmp_path / "second"
    source.mkdir()
    created = client.post(
        "/api/v1/libraries",
        json={
            "name": "Second",
            "source_path": str(source),
            "output_path": str(tmp_path / "second-clean"),
        },
    ).json()
    assert created["name"] == "Second"

    assert len(client.get("/api/v1/libraries").json()) == 2
    assert client.delete(f"/api/v1/libraries/{created['id']}").status_code == 200
    assert len(client.get("/api/v1/libraries").json()) == 1


def test_library_rejects_identical_paths(app_client, tmp_path):
    client, _ = app_client
    shared = tmp_path / "shared"
    shared.mkdir()
    response = client.post(
        "/api/v1/libraries",
        json={"name": "Bad", "source_path": str(shared), "output_path": str(shared)},
    )
    assert response.status_code == 400


def test_library_rejects_missing_source(app_client, tmp_path):
    client, _ = app_client
    response = client.post(
        "/api/v1/libraries",
        json={
            "name": "Bad",
            "source_path": str(tmp_path / "absent"),
            "output_path": str(tmp_path / "out"),
        },
    )
    assert response.status_code == 400


class TestProgressPayload:
    """A percentage that has not moved must still say the job is alive."""

    def _state(self, **overrides):
        state = {
            "job_id": 7,
            "progress": 0.76,
            "stage": "rendering",
            "message": "Encoding cleaned audio",
            "part_index": 1,
            "part_total": 1,
            "stage_fraction": 0.0,
            "started_at": 100.0,
            "stage_started_at": 400.0,
        }
        state.update(overrides)
        return state

    def test_reports_how_long_the_stage_has_been_running(self):
        from prudify.services.queue import _progress_payload

        payload = _progress_payload(self._state(), now=1000.0)
        assert payload["stage_elapsed"] == 600.0
        assert payload["elapsed"] == 900.0

    def test_no_eta_before_there_is_anything_to_extrapolate_from(self):
        from prudify.services.queue import _progress_payload

        payload = _progress_payload(self._state(stage_fraction=0.001), now=1000.0)
        assert payload["stage_eta_seconds"] is None

    def test_eta_once_the_stage_is_under_way(self):
        from prudify.services.queue import _progress_payload

        # A quarter done after ten minutes means thirty minutes to go.
        payload = _progress_payload(self._state(stage_fraction=0.25), now=1000.0)
        assert payload["stage_eta_seconds"] == 1800.0

    def test_the_heartbeat_thread_starts_with_the_queue(self, app_client):
        import threading

        names = {thread.name for thread in threading.enumerate()}
        assert "prudify-heartbeat" in names


def test_startup_sweeps_abandoned_work_directories(app_client):
    """A container killed mid-render leaves gigabytes nobody ever collects."""
    client, config = app_client
    from prudify.services.queue import JobQueue

    work = config.resolved_work_dir()
    abandoned = work / "job-999999" / "transcribe"
    abandoned.mkdir(parents=True)
    (abandoned / "audio.wav").write_bytes(b"\0" * 4096)
    unrelated = work / "not-a-job"
    unrelated.mkdir(parents=True, exist_ok=True)

    JobQueue(config)._sweep_stale_work_dirs()

    assert not (work / "job-999999").exists()
    assert unrelated.exists(), "only job-* directories are ours to delete"


def test_the_sweep_spares_work_still_queued(app_client):
    """Those directories hold the chunk cache that lets a requeued job resume."""
    client, config = app_client
    from prudify.services.queue import JobQueue

    client.post("/api/v1/books/scan")
    book_id = client.get("/api/v1/books").json()["items"][0]["id"]
    job_id = client.post(f"/api/v1/books/{book_id}/queue").json()["job_id"]

    live = config.resolved_work_dir() / f"job-{job_id}"
    live.mkdir(parents=True, exist_ok=True)
    (live / "chunk-0000.json").write_text("[]")

    JobQueue(config)._sweep_stale_work_dirs()

    assert live.exists()


def test_library_rejects_an_unwritable_output_path(app_client, tmp_path):
    client, _ = app_client
    source = tmp_path / "src"
    source.mkdir()
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory")
    response = client.post(
        "/api/v1/libraries",
        json={
            "name": "Bad",
            "source_path": str(source),
            "output_path": str(blocker / "clean"),
        },
    )
    assert response.status_code == 400
    assert "unusable" in response.json()["detail"]


def test_updating_a_library_revalidates_its_paths(app_client, tmp_path):
    """A library could be created good and then edited to point somewhere unusable."""
    client, _ = app_client
    source = tmp_path / "src2"
    source.mkdir()
    created = client.post(
        "/api/v1/libraries",
        json={
            "name": "Good",
            "source_path": str(source),
            "output_path": str(tmp_path / "good-clean"),
        },
    ).json()

    blocker = tmp_path / "blocked2"
    blocker.write_text("a file, not a directory")
    response = client.put(
        f"/api/v1/libraries/{created['id']}",
        json={
            "name": "Good",
            "source_path": str(source),
            "output_path": str(blocker / "clean"),
        },
    )
    assert response.status_code == 400


def test_library_listing_reports_writability(app_client):
    client, config = app_client
    Path(config.libraries[0].output_path).mkdir(parents=True, exist_ok=True)
    entry = client.get("/api/v1/libraries").json()[0]
    assert entry["output_exists"] is True
    assert entry["output_writable"] is True


def test_wordlist_read_and_shadow(app_client):
    client, _ = app_client
    bundled = client.get("/api/v1/wordlists/strict").json()
    assert bundled["builtin"] is True
    assert bundled["rule_count"] > 0

    saved = client.put("/api/v1/wordlists/strict", json={"content": "fuck\ncunt\n"}).json()
    assert saved["builtin"] is False
    assert saved["rule_count"] == 2
    assert client.get("/api/v1/wordlists/strict").json()["rule_count"] == 2


def test_wordlist_tester_highlights_matches(app_client):
    client, _ = app_client
    body = client.post(
        "/api/v1/wordlists/test",
        json={"text": "a classic fucking sentence in Scunthorpe"},
    ).json()
    assert body["match_count"] == 1
    matched = [token["text"] for token in body["tokens"] if token["matched"]]
    assert matched == ["fucking"]


def test_wordlist_name_is_sanitised(app_client):
    client, _ = app_client
    assert client.get("/api/v1/wordlists/..%2F..%2Fetc").status_code in (400, 404)


def test_browse_lists_directories(app_client, tmp_path):
    client, _ = app_client
    body = client.get("/api/v1/system/browse", params={"path": str(tmp_path)}).json()
    names = {entry["name"] for entry in body["entries"]}
    assert "audiobooks" in names


def test_browse_rejects_a_file(app_client, tmp_path):
    client, _ = app_client
    target = tmp_path / "file.txt"
    target.write_text("x")
    assert client.get("/api/v1/system/browse", params={"path": str(target)}).status_code == 400


def test_monitor_toggle(app_client):
    client, _ = app_client
    client.post("/api/v1/books/scan")
    book_id = client.get("/api/v1/books").json()["items"][0]["id"]
    body = client.post(f"/api/v1/books/{book_id}/monitor", params={"monitored": False}).json()
    assert body["monitored"] is False
    assert body["status"] == "ignored"


def test_reset_never_touches_the_source(app_client, tmp_path):
    client, _ = app_client
    client.post("/api/v1/books/scan")
    book_id = client.get("/api/v1/books").json()["items"][0]["id"]
    source_file = tmp_path / "audiobooks/Author/Book/Book.m4b"

    client.post(f"/api/v1/books/{book_id}/reset", params={"delete_output": True})
    assert source_file.exists()


class TestStaticFileTraversal:
    """Regression: the SPA catch-all served any file the process could read.

    The route is unauthenticated by necessity -- the app shell must load
    before anyone can log in -- and it joined the request path onto the static
    directory with no containment check. `Path("/static") / "/config/x"`
    discards the left side entirely, so `GET //config/config.yaml` returned
    the config file, which holds the API key in cleartext. That is a full
    authentication bypass, not merely a file read.
    """

    @pytest.mark.parametrize("path", [
        "//etc/passwd",
        "/../../etc/passwd",
        "../../../../etc/passwd",
        "//config/config.yaml",
        "/..%2f..%2fetc%2fpasswd",
    ])
    def test_traversal_never_escapes_the_static_tree(self, app_client, path):
        client, _config = app_client
        response = client.get(path)
        # Either the SPA shell or a 404 -- never file contents.
        assert response.status_code in (200, 404)
        body = response.text
        assert "root:x:0:0" not in body
        assert "api_key" not in body
