import json
from urllib.parse import parse_qs, urlparse

import pulse.archive as archive


def test_musicbrainz_search_filters_to_tagged_north_american_artists(monkeypatch):
    payload = {
        "artists": [
            {
                "id": "83b9cbe7-9857-49e2-ab8e-b57b01038103",
                "name": "North Star",
                "country": "CA",
                "area": {"name": "Canada"},
                "begin-area": {"name": "Toronto"},
                "tags": [{"name": "hard rock"}],
            },
            {
                "id": "83b9cbe7-9857-49e2-ab8e-b57b01038104",
                "name": "Unmatched",
                "country": "US",
                "tags": [{"name": "pop"}],
            },
            {
                "id": "83b9cbe7-9857-49e2-ab8e-b57b01038105",
                "name": "Outside",
                "country": "GB",
                "tags": [{"name": "hard rock"}],
            },
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return json.dumps(payload).encode()

    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        assert timeout == 10
        return FakeResponse()

    monkeypatch.setattr(archive, "urlopen", fake_urlopen)
    monkeypatch.setattr(archive.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)
    monkeypatch.setattr(archive, "_last_request_at", 0.0)

    candidates = archive.search_musicbrainz("Hard Rock", limit=10)

    assert [candidate["musicbrainz_id"] for candidate in candidates] == [
        "83b9cbe7-9857-49e2-ab8e-b57b01038103"
    ]
    assert candidates[0]["area"] == "Toronto"
    assert candidates[0]["source_url"] == (
        "https://musicbrainz.org/artist/83b9cbe7-9857-49e2-ab8e-b57b01038103"
    )
    query = parse_qs(urlparse(requests[0].full_url).query)
    assert query["limit"] == ["100"]
    assert "country:CA" in query["query"][0]
    assert requests[0].get_header("User-agent").startswith("FalCorPulse/")
