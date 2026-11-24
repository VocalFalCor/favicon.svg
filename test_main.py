import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at a throwaway database for each test
    monkeypatch.setattr(main, "DB_FILE", str(tmp_path / "test.db"))
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
