import asyncio
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from pulse.heat import heat_score, heat_visuals


ROOT = Path(__file__).parent
DB_FILE = os.getenv("PULSE_DB_FILE", str(ROOT / "messages.db"))
UPLOAD_DIR = Path(os.getenv("PULSE_UPLOAD_DIR", str(ROOT / "uploads")))
STATIC_DIR = ROOT / "static"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DAILY_LISTENS_BEFORE_VOTE_PROMPT = 50
ALLOWED_AUDIO_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}

bearer_scheme = HTTPBearer(auto_error=False)
pulse_event_loop: asyncio.AbstractEventLoop | None = None
pulse_subscribers: set[asyncio.Queue[str]] = set()


class MessageCreate(BaseModel):
    name: str
    message: str


class ArtistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    area: str = Field(min_length=1, max_length=100)
    genre: str = Field(default="", max_length=80)
    bio: str = Field(default="", max_length=500)

    @field_validator("name", "area")
    @classmethod
    def strip_and_validate(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value

    @field_validator("genre", "bio")
    @classmethod
    def strip_optional_fields(cls, value: str) -> str:
        return value.strip()


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with closing(connect_db()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                area TEXT NOT NULL,
                genre TEXT NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                heat_gain_hits INTEGER NOT NULL DEFAULT 0,
                heat_gain_last_result TEXT NOT NULL DEFAULT 'waiting',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                artist_id TEXT NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                storage_name TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS listens (
                id TEXT PRIMARY KEY,
                track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                profile_id TEXT NOT NULL,
                played_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS votes (
                id TEXT PRIMARY KEY,
                artist_id TEXT NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
                profile_id TEXT NOT NULL,
                voter_area TEXT NOT NULL,
                vote_day TEXT NOT NULL,
                voted_at TEXT NOT NULL,
                UNIQUE (artist_id, profile_id, vote_day)
            );
            CREATE INDEX IF NOT EXISTS listens_played_at_idx ON listens(played_at);
            CREATE INDEX IF NOT EXISTS listens_track_id_idx ON listens(track_id);
            CREATE INDEX IF NOT EXISTS votes_voted_at_idx ON votes(voted_at);
            """
        )
        artist_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(artists)").fetchall()
        }
        if "genre" not in artist_columns:
            connection.execute("ALTER TABLE artists ADD COLUMN genre TEXT NOT NULL DEFAULT ''")
        if "bio" not in artist_columns:
            connection.execute("ALTER TABLE artists ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
        if "heat_gain_hits" not in artist_columns:
            connection.execute(
                "ALTER TABLE artists ADD COLUMN heat_gain_hits INTEGER NOT NULL DEFAULT 0"
            )
        if "heat_gain_last_result" not in artist_columns:
            connection.execute(
                "ALTER TABLE artists ADD COLUMN heat_gain_last_result TEXT NOT NULL DEFAULT 'waiting'"
            )
        connection.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pulse_event_loop
    init_db()
    pulse_event_loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        pulse_event_loop = None
        pulse_subscribers.clear()


app = FastAPI(title="Pulse", description="Local music discovery and HeatScore API", lifespan=lifespan)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def profile_id() -> str:
    return os.getenv("PULSE_PROFILE_ID", "local-listener")


def profile_area() -> str:
    return os.getenv("PULSE_USER_AREA", "Local").strip()


def require_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    expected = os.getenv("PULSE_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="API writes are disabled until PULSE_API_TOKEN is configured")
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=401,
            detail="A valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def positive_setting(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _deliver_pulse_event(payload: str) -> None:
    for subscriber in tuple(pulse_subscribers):
        if subscriber.full():
            subscriber.get_nowait()
        subscriber.put_nowait(payload)


def publish_pulse_event(event_type: str) -> None:
    loop = pulse_event_loop
    if loop is None or loop.is_closed():
        return
    payload = json.dumps({"type": event_type, "at": timestamp(utc_now())})
    loop.call_soon_threadsafe(_deliver_pulse_event, payload)


def fetch_messages(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT id, name, message FROM messages ORDER BY rowid"
    ).fetchall()
    return [{"id": row["id"], "name": row["name"], "message": row["message"]} for row in rows]


@app.get("/", response_class=FileResponse)
def landing_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/player", response_class=FileResponse)
def heatflow_player() -> FileResponse:
    return FileResponse(STATIC_DIR / "player.html", media_type="text/html")


@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(ROOT / "favicon.svg", media_type="image/svg+xml")


@app.get("/messages")
def list_messages() -> list[dict]:
    with closing(connect_db()) as connection:
        return fetch_messages(connection)


@app.post("/messages")
def create_message(payload: MessageCreate) -> dict:
    message_id = str(uuid.uuid4())
    with closing(connect_db()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO messages (id, name, message) VALUES (?, ?, ?)",
                (message_id, payload.name, payload.message),
            )
            row = connection.execute(
                "SELECT id, name, message FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
    return {"id": row["id"], "name": row["name"], "message": row["message"]}


@app.delete("/messages/{message_id}")
def delete_message(message_id: str) -> dict:
    with closing(connect_db()) as connection:
        with connection:
            cursor = connection.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Message not found")
    return {"deleted": True, "id": message_id}


def artist_heat_metrics(
    connection: sqlite3.Connection, artist: sqlite3.Row, now: datetime
) -> tuple[int, int, float]:
    artist_id = artist["id"]
    recent_cutoff = timestamp(now - timedelta(hours=24))
    recent_listens = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM listens
        JOIN tracks ON tracks.id = listens.track_id
        WHERE tracks.artist_id = ? AND listens.played_at >= ?
        """,
        (artist_id, recent_cutoff),
    ).fetchone()["count"]
    local_votes = connection.execute(
        """
        SELECT COUNT(*) AS count FROM votes
        WHERE artist_id = ? AND voted_at >= ? AND lower(voter_area) = lower(?)
        """,
        (artist_id, recent_cutoff, artist["area"]),
    ).fetchone()["count"]
    last_signal = connection.execute(
        """
        SELECT MAX(played_at) AS played_at
        FROM listens JOIN tracks ON tracks.id = listens.track_id
        WHERE tracks.artist_id = ?
        """,
        (artist_id,),
    ).fetchone()["played_at"]
    signal_time = datetime.fromisoformat(last_signal or artist["created_at"])
    days_since_signal = max(0.0, (now - signal_time).total_seconds() / 86400)
    score = heat_score(recent_listens, local_votes, days_since_signal)
    return recent_listens, local_votes, score


def record_heat_check(
    connection: sqlite3.Connection,
    artist: sqlite3.Row,
    score_before: float,
    now: datetime,
) -> tuple[bool, int]:
    score_after = artist_heat_metrics(connection, artist, now)[2]
    increased = score_after > score_before
    result = "gain" if increased else "held"
    connection.execute(
        """
        UPDATE artists
        SET heat_gain_hits = heat_gain_hits + ?, heat_gain_last_result = ?
        WHERE id = ?
        """,
        (int(increased), result, artist["id"]),
    )
    hits = connection.execute(
        "SELECT heat_gain_hits FROM artists WHERE id = ?", (artist["id"],)
    ).fetchone()["heat_gain_hits"]
    return increased, hits


def artist_snapshot(connection: sqlite3.Connection, artist: sqlite3.Row, now: datetime) -> dict:
    artist_id = artist["id"]
    recent_listens, local_votes, score = artist_heat_metrics(connection, artist, now)
    visuals = heat_visuals(score)
    tracks = connection.execute(
        "SELECT id, title, created_at FROM tracks WHERE artist_id = ? ORDER BY created_at",
        (artist_id,),
    ).fetchall()
    return {
        "id": artist_id,
        "name": artist["name"],
        "area": artist["area"],
        "genre": artist["genre"],
        "bio": artist["bio"],
        "can_vote": artist["area"].casefold() == profile_area().casefold(),
        "heat_score": score,
        "heat_gain_hits": artist["heat_gain_hits"],
        "heat_gain_last_result": artist["heat_gain_last_result"],
        "recent_listens": recent_listens,
        "local_votes": local_votes,
        **visuals,
        "tracks": [dict(track) for track in tracks],
    }


@app.get("/api/pulse")
def get_pulse() -> dict:
    now = utc_now()
    with closing(connect_db()) as connection:
        artists = connection.execute("SELECT * FROM artists ORDER BY name COLLATE NOCASE").fetchall()
        snapshots = [artist_snapshot(connection, artist, now) for artist in artists]
    return {
        "heat_score_formula": "40% recent listens (24h, max 50) + 30% local votes (24h, max 10) + 30% freshness (30-day decay)",
        "artists": snapshots,
        "preview_nodes": [
            {"heat_score": score, **heat_visuals(score)} for score in (18, 55, 90)
        ],
    }


@app.post("/api/automations/pulse-summary", dependencies=[Depends(require_api_token)])
def create_pulse_summary() -> dict[str, str]:
    webhook_url = os.getenv("PULSE_N8N_WEBHOOK_URL", "").strip()
    parsed_url = urlparse(webhook_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise HTTPException(
            status_code=503,
            detail="Configure PULSE_N8N_WEBHOOK_URL with an active local n8n webhook",
        )

    pulse = get_pulse()
    artists = sorted(
        pulse["artists"],
        key=lambda artist: (-artist["heat_score"], artist["name"].casefold()),
    )
    payload = {
        "generated_at": timestamp(utc_now()),
        "artist_count": len(artists),
        "track_count": sum(len(artist["tracks"]) for artist in artists),
        "artists": [
            {
                "name": artist["name"],
                "area": artist["area"],
                "genre": artist["genre"],
                "heat_score": artist["heat_score"],
                "heat_gain_hits": artist["heat_gain_hits"],
                "recent_listens": artist["recent_listens"],
                "local_votes": artist["local_votes"],
                "track_count": len(artist["tracks"]),
            }
            for artist in artists[:5]
        ],
    }
    request = Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"The n8n workflow returned HTTP {error.code}",
        ) from error
    except (URLError, TimeoutError) as error:
        raise HTTPException(
            status_code=502,
            detail="Could not reach the configured n8n workflow",
        ) from error
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=502,
            detail="The n8n workflow returned invalid JSON",
        ) from error

    summary = result.get("summary") if isinstance(result, dict) else None
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise HTTPException(
            status_code=502,
            detail="The n8n workflow response must contain a summary of 1–2000 characters",
        )
    return {"summary": summary.strip()}


@app.get("/api/pulse/events")
async def pulse_events() -> StreamingResponse:
    async def event_stream():
        subscriber: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        pulse_subscribers.add(subscriber)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(subscriber.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: pulse\ndata: {payload}\n\n"
        finally:
            pulse_subscribers.discard(subscriber)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/artists", status_code=201, dependencies=[Depends(require_api_token)])
def create_artist(payload: ArtistCreate) -> dict:
    artist_id = str(uuid.uuid4())
    created_at = timestamp(utc_now())
    with closing(connect_db()) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO artists (id, name, area, genre, bio, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (artist_id, payload.name, payload.area, payload.genre, payload.bio, created_at),
            )
    publish_pulse_event("artist_added")
    return {
        "id": artist_id,
        "name": payload.name,
        "area": payload.area,
        "genre": payload.genre,
        "bio": payload.bio,
        "created_at": created_at,
    }


@app.post("/api/tracks", status_code=201, dependencies=[Depends(require_api_token)])
def upload_track(
    artist_id: str = Form(...),
    title: str = Form(..., min_length=1, max_length=160),
    rights_confirmed: bool = Form(...),
    file: UploadFile = File(...),
) -> dict:
    if not rights_confirmed:
        raise HTTPException(status_code=400, detail="Confirm you have rights to distribute this recording")
    title = title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Track title cannot be blank")
    extension = Path(file.filename or "").suffix.lower()
    media_type = ALLOWED_AUDIO_TYPES.get(extension)
    if media_type is None:
        raise HTTPException(status_code=415, detail="Unsupported audio format")
    contents = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio uploads are limited to 25 MB")
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded audio file is empty")

    with closing(connect_db()) as connection:
        artist = connection.execute("SELECT id FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if artist is None:
        raise HTTPException(status_code=404, detail="Artist not found")

    track_id = str(uuid.uuid4())
    storage_name = f"{track_id}{extension}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = UPLOAD_DIR / storage_name
    destination.write_bytes(contents)
    created_at = timestamp(utc_now())
    try:
        with closing(connect_db()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO tracks (id, artist_id, title, storage_name, media_type, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (track_id, artist_id, title, storage_name, media_type, created_at),
                )
    except sqlite3.Error:
        destination.unlink()
        raise
    publish_pulse_event("track_uploaded")
    return {"id": track_id, "artist_id": artist_id, "title": title, "created_at": created_at}


@app.get("/api/tracks/{track_id}/audio")
def get_track_audio(track_id: str) -> FileResponse:
    with closing(connect_db()) as connection:
        track = connection.execute(
            "SELECT storage_name, media_type FROM tracks WHERE id = ?",
            (track_id,),
        ).fetchone()
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found")
    path = UPLOAD_DIR / track["storage_name"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio file is missing")
    return FileResponse(path, media_type=track["media_type"], headers={"Content-Disposition": "inline"})


@app.post("/api/tracks/{track_id}/listen", dependencies=[Depends(require_api_token)])
def record_listen(track_id: str) -> dict:
    now = utc_now()
    day = now.date().isoformat()
    with closing(connect_db()) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            track = connection.execute(
                """
                SELECT artists.*
                FROM tracks JOIN artists ON artists.id = tracks.artist_id
                WHERE tracks.id = ?
                """,
                (track_id,),
            ).fetchone()
            if track is None:
                raise HTTPException(status_code=404, detail="Track not found")
            score_before = artist_heat_metrics(connection, track, now)[2]
            connection.execute(
                "INSERT INTO listens (id, track_id, profile_id, played_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), track_id, profile_id(), timestamp(now)),
            )
            heat_gain_hit, heat_gain_hits = record_heat_check(
                connection, track, score_before, now
            )
            listens_today = connection.execute(
                "SELECT COUNT(*) AS count FROM listens WHERE profile_id = ? AND substr(played_at, 1, 10) = ?",
                (profile_id(), day),
            ).fetchone()["count"]
            voted_today = connection.execute(
                """
                SELECT 1 FROM votes
                WHERE profile_id = ? AND vote_day = ? AND lower(voter_area) = lower(?)
                LIMIT 1
                """,
                (profile_id(), day, profile_area()),
            ).fetchone() is not None
    publish_pulse_event("listen_recorded")
    return {
        "track_id": track_id,
        "daily_listens": listens_today,
        "free_tier": "unlimited",
        "vote_required": False,
        "vote_prompt_due": listens_today > MAX_DAILY_LISTENS_BEFORE_VOTE_PROMPT and not voted_today,
        "heat_gain_hit": heat_gain_hit,
        "heat_gain_hits": heat_gain_hits,
    }


@app.post("/api/artists/{artist_id}/vote", dependencies=[Depends(require_api_token)])
def vote_for_artist(artist_id: str) -> dict:
    now = utc_now()
    area = profile_area()
    day = now.date().isoformat()
    with closing(connect_db()) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            artist = connection.execute(
                "SELECT * FROM artists WHERE id = ?",
                (artist_id,),
            ).fetchone()
            if artist is None:
                raise HTTPException(status_code=404, detail="Artist not found")
            if artist["area"].casefold() != area.casefold():
                raise HTTPException(status_code=403, detail="You can vote only for artists in your configured area")
            score_before = artist_heat_metrics(connection, artist, now)[2]
            try:
                connection.execute(
                    """
                    INSERT INTO votes (id, artist_id, profile_id, voter_area, vote_day, voted_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), artist_id, profile_id(), area, day, timestamp(now)),
                )
            except sqlite3.IntegrityError as error:
                raise HTTPException(status_code=409, detail="You already voted for this artist today") from error
            heat_gain_hit, heat_gain_hits = record_heat_check(
                connection, artist, score_before, now
            )
    publish_pulse_event("vote_cast")
    return {
        "artist_id": artist_id,
        "artist_name": artist["name"],
        "vote_recorded": True,
        "heat_gain_hit": heat_gain_hit,
        "heat_gain_hits": heat_gain_hits,
    }


@app.get("/api/profile", dependencies=[Depends(require_api_token)])
def get_profile() -> dict:
    day = utc_now().date().isoformat()
    current_profile = profile_id()
    with closing(connect_db()) as connection:
        listens = connection.execute(
            "SELECT COUNT(*) AS count FROM listens WHERE profile_id = ?",
            (current_profile,),
        ).fetchone()["count"]
        listens_today = connection.execute(
            "SELECT COUNT(*) AS count FROM listens WHERE profile_id = ? AND substr(played_at, 1, 10) = ?",
            (current_profile, day),
        ).fetchone()["count"]
        votes = connection.execute(
            "SELECT COUNT(*) AS count FROM votes WHERE profile_id = ?",
            (current_profile,),
        ).fetchone()["count"]

    listener_threshold = positive_setting("PULSE_BADGE_LISTENER_STREAMS", 10)
    scout_threshold = positive_setting("PULSE_BADGE_SCOUT_VOTES", 5)
    badges = [
        {
            "id": "listener",
            "name": "Listener",
            "description": f"Listen to {listener_threshold} tracks",
            "progress": min(listens, listener_threshold),
            "threshold": listener_threshold,
            "earned": listens >= listener_threshold,
        },
        {
            "id": "scout",
            "name": "Scout",
            "description": f"Cast {scout_threshold} local artist votes",
            "progress": min(votes, scout_threshold),
            "threshold": scout_threshold,
            "earned": votes >= scout_threshold,
        },
        {
            "id": "pulse-setter",
            "name": "Pulse Setter",
            "description": "Make your first HeatScore contribution",
            "progress": int(listens + votes > 0),
            "threshold": 1,
            "earned": listens + votes > 0,
        },
    ]
    return {
        "profile": os.getenv("PULSE_PROFILE_NAME", "Local Listener"),
        "tier": "free",
        "daily_listens": listens_today,
        "daily_listen_limit": None,
        "vote_prompt_after": MAX_DAILY_LISTENS_BEFORE_VOTE_PROMPT,
        "voting_required": False,
        "badges": badges,
    }
