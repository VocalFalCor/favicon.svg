# FalCor Pulse

Pulse is a local-first music discovery MVP. Its landing page renders SVG artist nodes whose size, glow, color, and pulse rate follow each artist's HeatScore. Artists can be registered and tracks uploaded only with a rights confirmation.

See the [FalCor Pulse proof-of-concept stack diagram](./docs/falcor-pulse-stack.md) for the architecture, configuration variables, and proposed agentic integration boundary.

## Run locally with Docker

1. Copy `.env.example` to `.env`.
2. Replace both sample values with separate random tokens. Generate each with `py -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Set `PULSE_USER_AREA` to the area used by this local listener profile.
4. Start the API and JupyterLab:

   ```powershell
   docker compose up --build -d
   ```

5. Open [http://localhost:8000](http://localhost:8000) for Pulse and [http://localhost:8888](http://localhost:8888) for JupyterLab. JupyterLab uses `JUPYTER_TOKEN` from `.env`.

Both ports are bound to localhost. The API token protects artist creation, uploads, listens, votes, and profile data. Enter the token in the landing page to use those controls; it stays in the current browser tab. Do not expose this single-profile development setup directly to the public internet.

The Compose API stores its SQLite database and authorized audio uploads in the persistent `pulse_data` volume. Jupyter notebooks use `jupyter_data`. Back up those volumes to make the installation recoverable. Set `PULSE_PROFILE_NAME`, `PULSE_PROFILE_ID`, and `PULSE_USER_AREA` in `.env` for the local profile. `PULSE_BADGE_LISTENER_STREAMS` and `PULSE_BADGE_SCOUT_VOTES` configure the Listener and Scout badge thresholds (defaults: 10 and 5).

## HeatScore and participation

HeatScore is a 0–100 score:

```text
40 × min(recent listens in 24h / 50, 1)
+ 30 × min(local votes in 24h / 10, 1)
+ 30 × max(1 - days since last signal / 30, 0)
```

The score maps to SVG node radius (12–48), a cyan-to-violet-to-pink heat color/glow, and a 4.5-to-1.1 second pulse. Each live node shows its score and artist name; the page refreshes the visualization every 30 seconds. With an empty catalog it shows clearly labeled sample nodes at 18, 55, and 90 so the scale is visible without inventing artists. Freshness uses the latest listen, or the artist's registration time before their first listen.

The free tier has unlimited listening. Starting with the 51st listen in a UTC day, the API can prompt the profile to vote for a local artist; voting is optional and never blocks listening. An artist is votable when its area matches `PULSE_USER_AREA`. This is a profile setting, not location verification.

Starter badges are:

- **Listener** — reach the configured lifetime listen threshold.
- **Scout** — cast the configured number of local artist votes.
- **Pulse Setter** — make the first listen or vote that contributes to HeatScore.

The initial release has one configured local profile and one server-side API token, not public multi-user accounts or paid tiers. Do not place commercial recordings in the library unless you have distribution rights. This MVP stores authorized uploads locally; it does not fetch tracks from a third-party music service.

## API

- `GET /api/pulse` — artist catalog, metrics, and visual properties.
- `POST /api/artists` — add an artist (`name`, `area`).
- `POST /api/tracks` — upload audio (`artist_id`, `title`, `rights_confirmed`, `file`); supported formats: MP3, WAV, OGG, FLAC, M4A, AAC; 25 MB maximum.
- `GET /api/tracks/{id}/audio` — stream an uploaded track.
- `POST /api/tracks/{id}/listen` — record a listen and report whether an optional vote prompt is due.
- `POST /api/artists/{id}/vote` — cast one vote per artist per UTC day.
- `GET /api/profile` — free-tier status and badge progress.

Mutating endpoints use `Authorization: Bearer <PULSE_API_TOKEN>`. Interactive API docs are available at `/docs`.

## Tests and development

```powershell
py -m pip install -r requirements-dev.txt
py -m pytest -q
```

To run without Docker, configure `PULSE_API_TOKEN` and optionally `PULSE_USER_AREA`, then start `py -m uvicorn main:app --reload`. Local defaults store the database beside `main.py` and uploads in `uploads/`.

The root `Dockerfile` builds the API image. `n8n-stack/docker-compose.yml` remains a separate n8n/Ollama stack.
