"""HTTP API behaviour, exercised through FastAPI's TestClient."""

from __future__ import annotations

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
