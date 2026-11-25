import os
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
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
        connection.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


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


def fetch_messages(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT id, name, message FROM messages ORDER BY rowid"
    ).fetchall()
    return [{"id": row["id"], "name": row["name"], "message": row["message"]} for row in rows]


@app.get("/", response_class=FileResponse)
def landing_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


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


def artist_snapshot(connection: sqlite3.Connection, artist: sqlite3.Row, now: datetime) -> dict:
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
            track = connection.execute(
                "SELECT id FROM tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if track is None:
                raise HTTPException(status_code=404, detail="Track not found")
            connection.execute(
                "INSERT INTO listens (id, track_id, profile_id, played_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), track_id, profile_id(), timestamp(now)),
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
    return {
        "track_id": track_id,
        "daily_listens": listens_today,
        "free_tier": "unlimited",
        "vote_required": False,
        "vote_prompt_due": listens_today > MAX_DAILY_LISTENS_BEFORE_VOTE_PROMPT and not voted_today,
    }


@app.post("/api/artists/{artist_id}/vote", dependencies=[Depends(require_api_token)])
def vote_for_artist(artist_id: str) -> dict:
    now = utc_now()
    area = profile_area()
    day = now.date().isoformat()
    with closing(connect_db()) as connection:
        with connection:
            artist = connection.execute(
                "SELECT id, name, area FROM artists WHERE id = ?",
                (artist_id,),
            ).fetchone()
            if artist is None:
                raise HTTPException(status_code=404, detail="Artist not found")
            if artist["area"].casefold() != area.casefold():
                raise HTTPException(status_code=403, detail="You can vote only for artists in your configured area")
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
    return {"artist_id": artist_id, "artist_name": artist["name"], "vote_recorded": True}


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
