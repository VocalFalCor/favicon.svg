from __future__ import annotations

import json
import threading
import time
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NORTH_AMERICA_COUNTRIES = {"CA", "MX", "US"}
ARCHIVE_GENRES = {
    "North American Hip-Hop & Drill": (
        '(tag:"hip hop" OR tag:"hip-hop" OR tag:drill)',
        {"hip hop", "hip-hop", "drill", "underground hip hop"},
    ),
    "Hard Rock": ('tag:"hard rock"', {"hard rock"}),
    "Blues": ("tag:blues", {"blues"}),
    "Underground": ("tag:underground", {"underground", "underground hip hop", "underground rap"}),
    "Indie": (
        '(tag:indie OR tag:"indie rock" OR tag:"indie pop")',
        {"indie", "indie rock", "indie pop"},
    ),
}

_REQUEST_INTERVAL_SECONDS = 1.0
_request_lock = threading.Lock()
_last_request_at = 0.0


def search_musicbrainz(genre: str, limit: int = 10) -> list[dict]:
    if genre not in ARCHIVE_GENRES:
        raise ValueError(f"Unsupported archive genre: {genre}")

    genre_query, accepted_tags = ARCHIVE_GENRES[genre]
    country_query = " OR ".join(f"country:{country}" for country in sorted(NORTH_AMERICA_COUNTRIES))
    query = f"{genre_query} AND ({country_query})"
    url = "https://musicbrainz.org/ws/2/artist/?" + urlencode(
        {"query": query, "fmt": "json", "limit": 100}
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "FalCorPulse/0.1 (https://github.com/VocalFalCor/favicon.svg)",
        },
    )

    global _last_request_at
    with _request_lock:
        delay = _REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if delay > 0:
            time.sleep(delay)
        _last_request_at = time.monotonic()
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())

    artists = payload.get("artists") if isinstance(payload, dict) else None
    if not isinstance(artists, list):
        raise ValueError("MusicBrainz response did not contain an artist list")

    candidates = []
    for artist in artists:
        if not isinstance(artist, dict):
            continue
        country = artist.get("country")
        musicbrainz_id = artist.get("id")
        name = artist.get("name")
        try:
            musicbrainz_id = str(uuid.UUID(musicbrainz_id))
        except (ValueError, TypeError, AttributeError):
            continue
        if (
            not isinstance(country, str)
            or country not in NORTH_AMERICA_COUNTRIES
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue

        tags = artist.get("tags", [])
        if not isinstance(tags, list):
            continue
        tag_names = {
            tag["name"].strip().casefold()
            for tag in tags
            if isinstance(tag, dict) and isinstance(tag.get("name"), str)
        }
        if not tag_names.intersection(accepted_tags):
            continue

        begin_area = artist.get("begin-area")
        begin_area = begin_area if isinstance(begin_area, dict) else {}
        country_area = artist.get("area")
        country_area = country_area if isinstance(country_area, dict) else {}
        area = begin_area.get("name") or country_area.get("name") or country
        if not isinstance(area, str):
            area = country
        disambiguation = artist.get("disambiguation", "")
        candidates.append(
            {
                "musicbrainz_id": musicbrainz_id,
                "name": name.strip(),
                "area": area,
                "country": country,
                "disambiguation": (
                    disambiguation if isinstance(disambiguation, str) else ""
                ),
                "tags": sorted(tag_names)[:20],
                "source_url": f"https://musicbrainz.org/artist/{musicbrainz_id}",
            }
        )
        if len(candidates) >= limit:
            break
    return candidates
