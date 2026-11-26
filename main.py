import asyncio
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from pulse.heat import heat_score, heat_visuals


ROOT = Path(__file__).parent
DB_FILE = os.getenv("PULSE_DB_FILE", str(ROOT / "messages.db"))
UPLOAD_DIR = Path(os.getenv("PULSE_UPLOAD_DIR", str(ROOT / "uploads")))
TOP5_EXPORT_DIR = Path(
    os.getenv("PULSE_TOP5_EXPORT_DIR", str(ROOT / "data" / "insights" / "top5"))
)
STATIC_DIR = ROOT / "static"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DAILY_LISTENS_BEFORE_VOTE_PROMPT = 50
TOP5_CONTEST_ID = "vibe-of-the-night"
ALLOWED_AUDIO_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}
SOURCE_PLATFORMS = {
    "youtube.com": "YouTube",
    "www.youtube.com": "YouTube",
    "m.youtube.com": "YouTube",
    "music.youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "soundcloud.com": "SoundCloud",
    "www.soundcloud.com": "SoundCloud",
    "m.soundcloud.com": "SoundCloud",
    "worldstar.com": "WorldStar",
    "www.worldstar.com": "WorldStar",
    "worldstarhiphop.com": "WorldStar",
    "www.worldstarhiphop.com": "WorldStar",
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


class SourceIntakeCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    artist_name: str = Field(default="", max_length=100)

    @field_validator("url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        value = value.strip()
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except ValueError as error:
            raise ValueError("Enter a valid public source URL") from error
        if (
            parsed.scheme != "https"
            or host not in SOURCE_PLATFORMS
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
        ):
            raise ValueError("Use an HTTPS link from YouTube, SoundCloud, or WorldStar")
        return value

    @field_validator("artist_name")
    @classmethod
    def strip_artist_name(cls, value: str) -> str:
        return value.strip()


class RightsReview(BaseModel):
    decision: str
    basis: str = Field(min_length=8, max_length=500)

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        decision = value.strip().lower()
        if decision not in {"cleared", "rejected"}:
            raise ValueError("Rights review decision must be cleared or rejected")
        return decision

    @field_validator("basis")
    @classmethod
    def strip_basis(cls, value: str) -> str:
        basis = value.strip()
        if len(basis) < 8:
            raise ValueError("Provide a brief rights-review basis")
        return basis


class FounderRsvp(BaseModel):
    response: str

    @field_validator("response")
    @classmethod
    def validate_response(cls, value: str) -> str:
        response = value.strip().lower()
        if response not in {"yes", "no"}:
            raise ValueError("RSVP response must be yes or no")
        return response


class Top5EntryCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=100)
    track_ids: list[str] = Field(min_length=5, max_length=5)

    @field_validator("display_name", "title")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value

    @field_validator("track_ids")
    @classmethod
    def validate_track_ids(cls, values: list[str]) -> list[str]:
        track_ids = [value.strip() for value in values]
        if any(not track_id for track_id in track_ids):
            raise ValueError("Choose five tracks")
        if len(set(track_ids)) != 5:
            raise ValueError("Choose five different tracks")
        return track_ids


class Top5Publish(BaseModel):
    winner_entry_id: str = Field(min_length=1)


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
                source_intake_id TEXT REFERENCES source_intakes(id),
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
            CREATE TABLE IF NOT EXISTS source_intakes (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                source_url TEXT NOT NULL,
                artist_name TEXT NOT NULL DEFAULT '',
                rights_status TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK (rights_status IN ('pending_review', 'cleared', 'rejected')),
                rights_basis TEXT NOT NULL DEFAULT '',
                rights_reviewed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS founder_invites (
                id TEXT PRIMARY KEY,
                source_intake_id TEXT NOT NULL REFERENCES source_intakes(id) ON DELETE CASCADE,
                response TEXT NOT NULL DEFAULT 'pending'
                    CHECK (response IN ('pending', 'yes', 'no')),
                created_at TEXT NOT NULL,
                responded_at TEXT
            );
            CREATE TABLE IF NOT EXISTS top5_contests (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'published')),
                winner_entry_id TEXT,
                created_at TEXT NOT NULL,
                published_at TEXT
            );
            CREATE TABLE IF NOT EXISTS top5_entries (
                id TEXT PRIMARY KEY,
                contest_id TEXT NOT NULL REFERENCES top5_contests(id) ON DELETE CASCADE,
                display_name TEXT NOT NULL,
                title TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS top5_entry_tracks (
                entry_id TEXT NOT NULL REFERENCES top5_entries(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 5),
                track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                PRIMARY KEY (entry_id, position),
                UNIQUE (entry_id, track_id)
            );
            CREATE TABLE IF NOT EXISTS top5_track_events (
                id TEXT PRIMARY KEY,
                entry_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                profile_id TEXT NOT NULL,
                played_at TEXT NOT NULL,
                FOREIGN KEY (entry_id, position)
                    REFERENCES top5_entry_tracks(entry_id, position) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS listens_played_at_idx ON listens(played_at);
            CREATE INDEX IF NOT EXISTS listens_track_id_idx ON listens(track_id);
            CREATE INDEX IF NOT EXISTS votes_voted_at_idx ON votes(voted_at);
            CREATE INDEX IF NOT EXISTS source_intakes_created_at_idx ON source_intakes(created_at);
            CREATE INDEX IF NOT EXISTS top5_track_events_entry_idx
                ON top5_track_events(entry_id, position);
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO top5_contests (id, title, prompt, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                TOP5_CONTEST_ID,
                "Top 5: Vibe of the Night",
                "Build a five-track sequence that sets the vibe. Use tracks already in the authorized Pulse library.",
                timestamp(utc_now()),
            ),
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
        track_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tracks)").fetchall()
        }
        if "source_intake_id" not in track_columns:
            connection.execute(
                "ALTER TABLE tracks ADD COLUMN source_intake_id TEXT REFERENCES source_intakes(id)"
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


@app.get("/founders/{invite_id}", response_class=FileResponse)
def founder_invite_page(invite_id: str) -> FileResponse:
    with closing(connect_db()) as connection:
        invite = connection.execute(
            "SELECT 1 FROM founder_invites WHERE id = ?",
            (invite_id,),
        ).fetchone()
    if invite is None:
        raise HTTPException(status_code=404, detail="Founder’s Circle invite not found")
    return FileResponse(STATIC_DIR / "founders.html", media_type="text/html")


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
    source_intake_id: str | None = Form(None),
) -> dict:
    if not rights_confirmed:
        raise HTTPException(status_code=400, detail="Confirm you have rights to distribute this recording")
    title = title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Track title cannot be blank")
    source_intake_id = source_intake_id.strip() if source_intake_id else None
    source_intake = None
    if source_intake_id:
        with closing(connect_db()) as connection:
            source_intake = connection.execute(
                "SELECT id, artist_name, rights_status FROM source_intakes WHERE id = ?",
                (source_intake_id,),
            ).fetchone()
        if source_intake is None:
            raise HTTPException(status_code=404, detail="Source intake not found")
        if source_intake["rights_status"] != "cleared":
            raise HTTPException(
                status_code=403,
                detail="Source intake must pass manual rights review before upload",
            )

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
        artist = connection.execute(
            "SELECT id, name FROM artists WHERE id = ?",
            (artist_id,),
        ).fetchone()
    if artist is None:
        raise HTTPException(status_code=404, detail="Artist not found")
    if (
        source_intake
        and source_intake["artist_name"]
        and source_intake["artist_name"].casefold() != artist["name"].casefold()
    ):
        raise HTTPException(
            status_code=400,
            detail="Selected artist must match the source intake artist name",
        )

    track_id = str(uuid.uuid4())
    storage_name = f"{track_id}{extension}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = UPLOAD_DIR / storage_name
    created_at = timestamp(utc_now())
    try:
        with closing(connect_db()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if source_intake_id:
                    reviewed_source = connection.execute(
                        "SELECT rights_status FROM source_intakes WHERE id = ?",
                        (source_intake_id,),
                    ).fetchone()
                    if reviewed_source is None:
                        raise HTTPException(status_code=404, detail="Source intake not found")
                    if reviewed_source["rights_status"] != "cleared":
                        raise HTTPException(
                            status_code=403,
                            detail="Source intake must pass manual rights review before upload",
                        )
                destination.write_bytes(contents)
                connection.execute(
                    """
                    INSERT INTO tracks (
                        id, artist_id, source_intake_id, title, storage_name, media_type, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        track_id,
                        artist_id,
                        source_intake_id,
                        title,
                        storage_name,
                        media_type,
                        created_at,
                    ),
                )
    except (sqlite3.Error, HTTPException, OSError):
        if destination.exists():
            destination.unlink()
        raise
    publish_pulse_event("track_uploaded")
    return {
        "id": track_id,
        "artist_id": artist_id,
        "source_intake_id": source_intake_id,
        "title": title,
        "created_at": created_at,
    }


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
    return _record_listen(track_id)


@app.post(
    "/api/top5/entries/{entry_id}/tracks/{position}/listen",
    dependencies=[Depends(require_api_token)],
)
def record_top5_listen(entry_id: str, position: int) -> dict:
    if position < 1 or position > 5:
        raise HTTPException(status_code=422, detail="Top 5 track position must be from 1 to 5")
    with closing(connect_db()) as connection:
        entry_track = connection.execute(
            """
            SELECT tet.track_id FROM top5_entry_tracks tet
            JOIN top5_entries te ON te.id = tet.entry_id
            WHERE te.id = ? AND tet.position = ?
            """,
            (entry_id, position),
        ).fetchone()
    if entry_track is None:
        raise HTTPException(status_code=404, detail="Top 5 track position not found")
    return _record_listen(entry_track["track_id"], entry_id, position)


def get_top5_payload() -> dict:
    with closing(connect_db()) as connection:
        contest = connection.execute(
            "SELECT * FROM top5_contests WHERE id = ?",
            (TOP5_CONTEST_ID,),
        ).fetchone()
        if contest is None:
            raise HTTPException(status_code=404, detail="Top 5 contest not found")
        entry_rows = connection.execute(
            """
            SELECT id, display_name, title, created_at
            FROM top5_entries
            WHERE contest_id = ?
            ORDER BY created_at DESC, id
            """,
            (TOP5_CONTEST_ID,),
        ).fetchall()
        entries = []
        for entry in entry_rows:
            track_rows = connection.execute(
                """
                SELECT tet.position, t.id AS track_id, t.title AS track_title,
                       a.name AS artist_name, a.area,
                       COUNT(tte.id) AS plays
                FROM top5_entry_tracks tet
                JOIN tracks t ON t.id = tet.track_id
                JOIN artists a ON a.id = t.artist_id
                LEFT JOIN top5_track_events tte
                    ON tte.entry_id = tet.entry_id
                    AND tte.position = tet.position
                WHERE tet.entry_id = ?
                GROUP BY tet.position, t.id, t.title, a.name, a.area
                ORDER BY tet.position
                """,
                (entry["id"],),
            ).fetchall()
            tracks = [
                {
                    "position": track["position"],
                    "track_id": track["track_id"],
                    "title": track["track_title"],
                    "artist_name": track["artist_name"],
                    "area": track["area"],
                    "plays": track["plays"],
                }
                for track in track_rows
            ]
            total_plays = sum(track["plays"] for track in tracks)
            third_track = next(track for track in tracks if track["position"] == 3)
            first_track = next(track for track in tracks if track["position"] == 1)
            second_track = next(track for track in tracks if track["position"] == 2)
            spark = {
                "artist_pair": [first_track["artist_name"], second_track["artist_name"]],
                "third_track": third_track["title"],
                "third_track_artist": third_track["artist_name"],
                "position_three_plays": third_track["plays"],
                "total_top5_plays": total_plays,
                "play_share_percent": round(
                    third_track["plays"] / total_plays * 100,
                    1,
                )
                if total_plays
                else 0,
            }
            entries.append(
                {
                    "id": entry["id"],
                    "display_name": entry["display_name"],
                    "title": entry["title"],
                    "created_at": entry["created_at"],
                    "tracks": tracks,
                    "spark": spark,
                }
            )
        winner = next(
            (entry for entry in entries if entry["id"] == contest["winner_entry_id"]),
            None,
        )
    return {
        "contest": {
            "id": contest["id"],
            "title": contest["title"],
            "prompt": contest["prompt"],
            "status": contest["status"],
            "winner_entry_id": contest["winner_entry_id"],
            "published_at": contest["published_at"],
            "winner": winner,
        },
        "entries": entries,
        "spark_definition": (
            "Descriptive position-3 play share, grouped with the artists in positions 1 and 2; "
            "it is not evidence that those artists caused the engagement."
        ),
    }


def export_top5_results(payload: dict) -> None:
    try:
        TOP5_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = TOP5_EXPORT_DIR / f"{payload['contest']['id']}.json"
        temporary = TOP5_EXPORT_DIR / f".{payload['contest']['id']}-{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
    except OSError as error:
        raise HTTPException(
            status_code=500,
            detail="Results were published, but the Top 5 archive could not be written",
        ) from error


@app.get("/api/top5")
def get_top5_contest() -> dict:
    return get_top5_payload()


@app.post(
    "/api/top5/entries",
    status_code=201,
    dependencies=[Depends(require_api_token)],
)
def create_top5_entry(payload: Top5EntryCreate) -> dict:
    entry_id = str(uuid.uuid4())
    created_at = timestamp(utc_now())
    with closing(connect_db()) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            contest = connection.execute(
                "SELECT status FROM top5_contests WHERE id = ?",
                (TOP5_CONTEST_ID,),
            ).fetchone()
            if contest is None:
                raise HTTPException(status_code=404, detail="Top 5 contest not found")
            if contest["status"] != "open":
                raise HTTPException(status_code=409, detail="This Top 5 contest is closed")
            placeholders = ",".join("?" for _ in payload.track_ids)
            tracks = connection.execute(
                f"SELECT id FROM tracks WHERE id IN ({placeholders})",
                payload.track_ids,
            ).fetchall()
            if len(tracks) != 5:
                raise HTTPException(
                    status_code=422,
                    detail="Choose five different tracks from the authorized Pulse library",
                )
            connection.execute(
                """
                INSERT INTO top5_entries
                    (id, contest_id, display_name, title, profile_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    TOP5_CONTEST_ID,
                    payload.display_name,
                    payload.title,
                    profile_id(),
                    created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO top5_entry_tracks (entry_id, position, track_id)
                VALUES (?, ?, ?)
                """,
                [
                    (entry_id, position, track_id)
                    for position, track_id in enumerate(payload.track_ids, start=1)
                ],
            )
    return next(
        entry for entry in get_top5_payload()["entries"] if entry["id"] == entry_id
    )


@app.post(
    "/api/top5/publish",
    dependencies=[Depends(require_api_token)],
)
def publish_top5_results(payload: Top5Publish) -> dict:
    published_at = timestamp(utc_now())
    with closing(connect_db()) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            contest = connection.execute(
                "SELECT status, winner_entry_id FROM top5_contests WHERE id = ?",
                (TOP5_CONTEST_ID,),
            ).fetchone()
            if contest is None:
                raise HTTPException(status_code=404, detail="Top 5 contest not found")
            winner = connection.execute(
                "SELECT id FROM top5_entries WHERE id = ? AND contest_id = ?",
                (payload.winner_entry_id, TOP5_CONTEST_ID),
            ).fetchone()
            if winner is None:
                raise HTTPException(status_code=404, detail="Top 5 entry not found")
            if contest["status"] == "published":
                if contest["winner_entry_id"] != payload.winner_entry_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Published results cannot be changed",
                    )
            else:
                connection.execute(
                    """
                    UPDATE top5_contests
                    SET status = 'published', winner_entry_id = ?, published_at = ?
                    WHERE id = ?
                    """,
                    (payload.winner_entry_id, published_at, TOP5_CONTEST_ID),
                )
    result = get_top5_payload()
    export_top5_results(result)
    return result


def _record_listen(
    track_id: str,
    top5_entry_id: str | None = None,
    top5_position: int | None = None,
) -> dict:
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
            if top5_entry_id is not None and top5_position is not None:
                entry_track = connection.execute(
                    """
                    SELECT 1 FROM top5_entry_tracks
                    WHERE entry_id = ? AND position = ? AND track_id = ?
                    """,
                    (top5_entry_id, top5_position, track_id),
                ).fetchone()
                if entry_track is None:
                    raise HTTPException(status_code=404, detail="Top 5 track position not found")
            score_before = artist_heat_metrics(connection, track, now)[2]
            played_at = timestamp(now)
            connection.execute(
                "INSERT INTO listens (id, track_id, profile_id, played_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), track_id, profile_id(), played_at),
            )
            if top5_entry_id is not None and top5_position is not None:
                connection.execute(
                    """
                    INSERT INTO top5_track_events
                        (id, entry_id, position, track_id, profile_id, played_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        top5_entry_id,
                        top5_position,
                        track_id,
                        profile_id(),
                        played_at,
                    ),
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
    result = {
        "track_id": track_id,
        "daily_listens": listens_today,
        "free_tier": "unlimited",
        "vote_required": False,
        "vote_prompt_due": listens_today > MAX_DAILY_LISTENS_BEFORE_VOTE_PROMPT and not voted_today,
        "heat_gain_hit": heat_gain_hit,
        "heat_gain_hits": heat_gain_hits,
    }
    if top5_entry_id is not None and top5_position is not None:
        result["top5_entry_id"] = top5_entry_id
        result["top5_position"] = top5_position
    return result


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


@app.post("/api/source-intakes", status_code=201, dependencies=[Depends(require_api_token)])
def create_source_intake(payload: SourceIntakeCreate) -> dict:
    host = urlsplit(payload.url).hostname
    if host is None:
        raise HTTPException(status_code=422, detail="Enter a valid public source URL")
    platform = SOURCE_PLATFORMS.get(host.lower().rstrip("."))
    if platform is None:
        raise HTTPException(
            status_code=422,
            detail="Use an HTTPS link from YouTube, SoundCloud, or WorldStar",
        )
    source_id = str(uuid.uuid4())
    invite_id = str(uuid.uuid4())
    created_at = timestamp(utc_now())
    with closing(connect_db()) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO source_intakes (id, platform, source_url, artist_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, platform, payload.url, payload.artist_name, created_at),
            )
            connection.execute(
                """
                INSERT INTO founder_invites (id, source_intake_id, created_at)
                VALUES (?, ?, ?)
                """,
                (invite_id, source_id, created_at),
            )
    return {
        "id": source_id,
        "platform": platform,
        "source_url": payload.url,
        "artist_name": payload.artist_name,
        "rights_status": "pending_review",
        "created_at": created_at,
        "founder_invite_path": f"/founders/{invite_id}",
    }


@app.get(
    "/api/source-intakes/{source_id}",
    dependencies=[Depends(require_api_token)],
)
def get_source_intake(source_id: str) -> dict:
    with closing(connect_db()) as connection:
        source = connection.execute(
            """
            SELECT id, platform, source_url, artist_name, rights_status,
                   rights_basis, rights_reviewed_at, created_at
            FROM source_intakes
            WHERE id = ?
            """,
            (source_id,),
        ).fetchone()
    if source is None:
        raise HTTPException(status_code=404, detail="Source intake not found")
    return dict(source)


@app.post(
    "/api/source-intakes/{source_id}/rights-review",
    dependencies=[Depends(require_api_token)],
)
def review_source_intake(source_id: str, payload: RightsReview) -> dict:
    reviewed_at = timestamp(utc_now())
    with closing(connect_db()) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE source_intakes
                SET rights_status = ?, rights_basis = ?, rights_reviewed_at = ?
                WHERE id = ?
                """,
                (payload.decision, payload.basis, reviewed_at, source_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Source intake not found")
    return {
        "id": source_id,
        "rights_status": payload.decision,
        "rights_basis": payload.basis,
        "rights_reviewed_at": reviewed_at,
    }


@app.get("/api/founders/{invite_id}")
def get_founder_invite(invite_id: str) -> dict:
    with closing(connect_db()) as connection:
        invite = connection.execute(
            """
            SELECT fi.id, fi.response, fi.created_at, fi.responded_at,
                   si.platform, si.source_url, si.artist_name, si.rights_status
            FROM founder_invites fi
            JOIN source_intakes si ON si.id = fi.source_intake_id
            WHERE fi.id = ?
            """,
            (invite_id,),
        ).fetchone()
    if invite is None:
        raise HTTPException(status_code=404, detail="Founder’s Circle invite not found")
    return {
        "invite_id": invite["id"],
        "response": invite["response"],
        "created_at": invite["created_at"],
        "responded_at": invite["responded_at"],
        "source": {
            "platform": invite["platform"],
            "url": invite["source_url"],
            "artist_name": invite["artist_name"],
            "rights_status": invite["rights_status"],
        },
        "offerings": [
            "Founder’s Circle community recognition",
            "Early previews of local HeatFlow playlists and HeatScore insights",
            "Artist-feature nominations and community feedback sessions",
            "A voice in shaping the local Pulse experience",
        ],
        "program_note": "Independent community program; RSVP does not grant audio or distribution rights and does not guarantee promotion.",
    }


@app.post("/api/founders/{invite_id}/rsvp")
def respond_to_founder_invite(invite_id: str, payload: FounderRsvp) -> dict:
    responded_at = timestamp(utc_now())
    with closing(connect_db()) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE founder_invites
                SET response = ?, responded_at = ?
                WHERE id = ?
                """,
                (payload.response, responded_at, invite_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Founder’s Circle invite not found")
    return {"invite_id": invite_id, "response": payload.response, "responded_at": responded_at}


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
