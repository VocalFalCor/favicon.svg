import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at a throwaway database for each test
    monkeypatch.setattr(main, "DB_FILE", str(tmp_path / "test.db"))
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    main.init_db()
    return TestClient(main.app)


def create(client, name="Jasen", message="hello"):
    r = client.post("/messages", json={"name": name, "message": message})
    assert r.status_code == 200
    return r.json()


def test_list_starts_empty(client):
    r = client.get("/messages")
    assert r.status_code == 200
    assert r.json() == []


def test_create_and_list(client):
    created = create(client, message="SQLite test")
    r = client.get("/messages")
    assert r.json() == [created]


def test_delete_existing(client):
    created = create(client)
    r = client.delete(f"/messages/{created['id']}")
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "id": created["id"]}
    assert client.get("/messages").json() == []


def test_delete_missing_returns_404(client):
    r = client.delete("/messages/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "Message not found"}


def test_delete_twice_second_is_404(client):
    created = create(client)
    assert client.delete(f"/messages/{created['id']}").status_code == 200
    assert client.delete(f"/messages/{created['id']}").status_code == 404


def test_delete_only_removes_target(client):
    a = create(client, message="a")
    b = create(client, message="b")
    client.delete(f"/messages/{a['id']}")
    assert client.get("/messages").json() == [b]


def test_no_lock_after_many_requests(client):
    # Regression test for "database is locked": connections must close
    ids = [create(client, message=str(i))["id"] for i in range(25)]
    for _ in range(5):
        client.get("/messages")
    for message_id in ids:
        assert client.delete(f"/messages/{message_id}").status_code == 200
    assert client.get("/messages").json() == []


def test_create_rejects_bad_body(client):
    r = client.post("/messages", json={"name": "only name"})
    assert r.status_code == 422


def test_landing_page_and_empty_pulse(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "local pulse" in page.text
    assert client.get("/favicon.svg").headers["content-type"] == "image/svg+xml"
    pulse = client.get("/api/pulse").json()
    assert pulse["artists"] == []
    assert [node["heat_score"] for node in pulse["preview_nodes"]] == [18, 55, 90]


def test_artist_writes_require_token_and_votes_are_local_and_daily(client, monkeypatch):
    monkeypatch.delenv("PULSE_API_TOKEN", raising=False)
    assert client.post("/api/artists", json={"name": "Echo", "area": "Toronto"}).status_code == 503

    monkeypatch.setenv("PULSE_API_TOKEN", "test-token")
    monkeypatch.setenv("PULSE_USER_AREA", "Toronto")
    headers = {"Authorization": "Bearer test-token"}
    assert client.post(
        "/api/artists",
        json={"name": "Invalid", "area": "Toronto"},
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 401
    artist = client.post(
        "/api/artists",
        json={"name": "Echo", "area": "Toronto"},
        headers=headers,
    )
    assert artist.status_code == 201
    artist_id = artist.json()["id"]

    outside_area = client.post(
        "/api/artists",
        json={"name": "Elsewhere", "area": "Montreal"},
        headers=headers,
    ).json()
    assert client.post(f"/api/artists/{outside_area['id']}/vote", headers=headers).status_code == 403
    assert client.post(f"/api/artists/{artist_id}/vote", headers=headers).json()["vote_recorded"]
    assert client.post(f"/api/artists/{artist_id}/vote", headers=headers).status_code == 409
    profile = client.get("/api/profile", headers=headers).json()
    assert profile["tier"] == "free"
    assert profile["voting_required"] is False
    assert profile["badges"][1]["progress"] == 1
    assert profile["badges"][2]["earned"] is True


def test_upload_requires_rights_and_supported_format(client, monkeypatch):
    monkeypatch.setenv("PULSE_API_TOKEN", "test-token")
    headers = {"Authorization": "Bearer test-token"}
    artist = client.post(
        "/api/artists",
        json={"name": "Echo", "area": "Local"},
        headers=headers,
    ).json()
    form = {"artist_id": artist["id"], "title": "Track"}
    denied = client.post(
        "/api/tracks",
        headers=headers,
        data={**form, "rights_confirmed": "false"},
        files={"file": ("track.mp3", b"audio", "audio/mpeg")},
    )
    assert denied.status_code == 400

    unsupported = client.post(
        "/api/tracks",
        headers=headers,
        data={**form, "rights_confirmed": "true"},
        files={"file": ("track.txt", b"audio", "text/plain")},
    )
    assert unsupported.status_code == 415


def test_upload_stream_and_optional_vote_prompt(client, monkeypatch):
    monkeypatch.setenv("PULSE_API_TOKEN", "test-token")
    monkeypatch.setenv("PULSE_USER_AREA", "Toronto")
    monkeypatch.setenv("PULSE_BADGE_LISTENER_STREAMS", "2")
    headers = {"Authorization": "Bearer test-token"}
    artist = client.post(
        "/api/artists",
        json={"name": "Echo", "area": "Toronto"},
        headers=headers,
    ).json()
    upload = client.post(
        "/api/tracks",
        headers=headers,
        data={
            "artist_id": artist["id"],
            "title": "First Light",
            "rights_confirmed": "true",
        },
        files={"file": ("first-light.mp3", b"sample audio", "audio/mpeg")},
    )
    assert upload.status_code == 201
    track_id = upload.json()["id"]
    assert client.get(f"/api/tracks/{track_id}/audio").content == b"sample audio"

    for _ in range(50):
        response = client.post(f"/api/tracks/{track_id}/listen", headers=headers)
        assert response.status_code == 200
        assert response.json()["vote_required"] is False
        assert response.json()["vote_prompt_due"] is False

    over_threshold = client.post(f"/api/tracks/{track_id}/listen", headers=headers)
    assert over_threshold.json()["daily_listens"] == 51
    assert over_threshold.json()["vote_prompt_due"] is True

    pulse = client.get("/api/pulse").json()
    assert pulse["artists"][0]["tracks"][0]["id"] == track_id
    assert pulse["artists"][0]["heat_score"] == 70.0
    assert pulse["artists"][0]["node_radius"] > 12

    assert client.post(f"/api/artists/{artist['id']}/vote", headers=headers).status_code == 200
    after_vote = client.post(f"/api/tracks/{track_id}/listen", headers=headers).json()
    assert after_vote["daily_listens"] == 52
    assert after_vote["vote_prompt_due"] is False
    profile = client.get("/api/profile", headers=headers).json()
    assert profile["badges"][0]["earned"] is True
    assert profile["badges"][0]["threshold"] == 2
