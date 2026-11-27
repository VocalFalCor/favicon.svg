# FalCor Pulse

Pulse is a local-first music discovery MVP. Its landing page renders SVG artist nodes whose size, glow, color, and pulse rate follow each artist's HeatScore. Artists can be registered and tracks uploaded only with a rights confirmation.

See the [FalCor Pulse proof-of-concept stack diagram](./docs/falcor-pulse-stack.md) for the editable architecture and variable inventory, or the [standalone SVG export](./docs/falcor-pulse-stack.svg).

## Run locally with Docker

1. Copy `.env.example` to `.env`.
2. Replace both sample values with separate random tokens. Generate each with `py -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Set `PULSE_USER_AREA` to the area used by this local listener profile.
4. Start the API and JupyterLab:

   ```powershell
   docker compose up --build -d
   ```

5. Open [http://localhost:8000](http://localhost:8000) for Pulse and [http://localhost:8888](http://localhost:8888) for JupyterLab. JupyterLab uses `JUPYTER_TOKEN` from `.env`.

Both ports are bound to localhost. The API token protects artist creation, uploads, listens, votes, source intake, and profile data. Enter the token in the landing page to use those controls; it is passed only in memory to the same-origin HeatFlow popup and is never saved in playlist storage or a URL. Do not expose this single-profile development setup directly to the public internet.

The Compose API stores its SQLite database and authorized audio uploads in the persistent `pulse_data` volume. Jupyter notebooks use `jupyter_data`. Back up those volumes to make the installation recoverable. Set `PULSE_PROFILE_NAME`, `PULSE_PROFILE_ID`, and `PULSE_USER_AREA` in `.env` for the local profile. `PULSE_BADGE_LISTENER_STREAMS` and `PULSE_BADGE_SCOUT_VOTES` configure the Listener and Scout badge thresholds (defaults: 10 and 5).

## HeatScore and participation

HeatScore is a 0–100 score:

```text
40 × min(recent listens in 24h / 50, 1)
+ 30 × min(local votes in 24h / 10, 1)
+ 30 × max(1 - days since last signal / 30, 0)
```

The score maps to SVG node radius (12–48), a cyan-to-violet-to-pink heat color/glow, and a 4.5-to-1.1 second pulse. Each live node shows its score and artist name. LIVEPULSE™ sends server-sent events after artist, track, listen, and vote changes; 30-second polling remains as a reconnect fallback. With an empty catalog the page shows clearly labeled sample nodes at 18, 55, and 90, not invented artists. Freshness uses the latest listen, or the artist's registration time before their first listen.

Each artist also has a persistent **Heat gain hits** counter. On every recorded listen or local vote, Pulse compares that artist's HeatScore immediately before and after the signal: a rise adds one hit; a flat or lower score holds the count without resetting it. The page shows the cumulative hits and whether the last check gained or held. Refreshes, track uploads, and time-based score decay do not count as signal checks.

**HeatFlow** lets listeners build a named playlist from tracks already in the Pulse library. The queue is saved in browser-local storage; its separate popup shows track, artist, hometown/area, elapsed time, and duration, with play, ±10-second seek, shuffle, and erase-from-queue controls. Erasing never deletes the library upload. Playing a track records a listen and requires the API token.

## Top 5: Vibe of the Night

The local contest lets a token-authorized listener submit an ordered five-track sequence from the Pulse library, give it a public display name/title, and listen through each position. Library uploads require the uploader's rights confirmation; source-linked uploads additionally require an operator-cleared intake. An organizer chooses and publishes a winner; audience voting and verified client accounts are not implemented, so this is not a public fair-voting system.

Top 5 listens are recorded against their playlist position. The **Spark** signal is the position-3 track's share of recorded plays, shown alongside the artists in positions 1 and 2. It is descriptive co-occurrence, not a causal claim, quality score, or artist ranking. Contest entries and anonymous profile-keyed play events stay in SQLite; publishing writes a JSON snapshot under `data/insights/top5/` on the host, or `/data/insights/top5/` in Compose (git-ignored). Display names are unverified, and entries require the shared local API token.

## Independent source intake and Founder’s Circle

The source intake accepts public links from YouTube, SoundCloud, or WorldStar and creates a manual rights-review lead plus an opt-in Founder’s Circle RSVP link. This MVP stores the URL and optional artist name only: it does not fetch platform metadata, scrape pages, download audio, or send invitation emails. A public link or RSVP is not permission to distribute a recording. An operator must separately verify permission and record a brief, non-sensitive review basis; Pulse records that decision but does not independently verify legal documents. The upload API blocks a linked pending/rejected intake and requires the existing uploader rights confirmation.

The RSVP page describes the POC offerings: community recognition, early HeatFlow/HeatScore previews, artist-feature nominations, and a voice in shaping Pulse. It collects no email and makes no guaranteed-promotion or pay-to-play promise. For the source intake agent instructions and safety requirements, see [FalCor source-intake task prompt](./docs/falcor-source-intake-agent-prompt.md).

## North American artist archive discovery

The archive discovery agent searches the public MusicBrainz artist catalog for up to 10 candidates in each of five tags: North American Hip-Hop & Drill, Hard Rock, Blues, Underground, and Indie. It limits results to the United States, Canada, and Mexico, records MusicBrainz attribution and tags, and stages candidates in a token-protected review queue. MusicBrainz is a metadata catalog; its community tags can be incomplete or inaccurate, so an operator reviews each candidate before adding an artist profile.

Discovery only creates artist metadata. It does not create tracks, fetch audio, clear distribution rights, contact artists, or guarantee that a candidate is independent or active. A profile approval is not rights approval. Tracks still need a separately reviewed source intake and the existing uploader rights confirmation. MusicBrainz requests use the official [Web Service API](https://musicbrainz.org/doc/MusicBrainz_API), its [search API](https://musicbrainz.org/doc/MusicBrainz_API/Search), and a descriptive User-Agent; calls are serialized to respect its one-request-per-second limit. Review MusicBrainz's current service terms before any commercial deployment.

For the discovery-agent operating contract, see [FalCor music archive agent prompt](./docs/falcor-music-archive-agent-prompt.md).

The free tier has unlimited listening. Starting with the 51st listen in a UTC day, the API can prompt the profile to vote for a local artist; voting is optional and never blocks listening. An artist is votable when its area matches `PULSE_USER_AREA`. This is a profile setting, not location verification.

Starter badges are:

- **Listener** — reach the configured lifetime listen threshold.
- **Scout** — cast the configured number of local artist votes.
- **Pulse Setter** — make the first listen or vote that contributes to HeatScore.

The initial release has one configured local profile and one server-side API token, not public multi-user accounts or paid tiers. Do not place commercial recordings in the library unless you have distribution rights. This MVP stores authorized uploads locally; it does not fetch tracks from a third-party music service.

## API

- `GET /api/pulse` — artist catalog, metrics, visual properties, and per-artist Heat gain hits / last-check status.
- `GET /api/pulse/events` — public server-sent event stream; event payloads contain only change type and timestamp.
- `POST /api/artists` — add an artist (`name`, `area`, optional `genre` and `bio`).
- `POST /api/tracks` — upload audio (`artist_id`, `title`, `rights_confirmed`, `file`, optional cleared `source_intake_id`); supported formats: MP3, WAV, OGG, FLAC, M4A, AAC; 25 MB maximum.
- `GET /api/tracks/{id}/audio` — stream an uploaded track.
- `POST /api/tracks/{id}/listen` — record a listen, report whether an optional vote prompt is due, and return the Heat gain check result.
- `POST /api/artists/{id}/vote` — cast one vote per artist per UTC day and return the Heat gain check result.
- `GET /api/top5` / `POST /api/top5/entries` — view the local contest and submit an ordered Top 5 using five distinct tracks from the Pulse library.
- `POST /api/top5/entries/{entry_id}/tracks/{position}/listen` — record a Top 5 listen with its position for the Spark aggregate.
- `POST /api/top5/publish` — organizer-select a winner and archive the published result to `data/insights/top5/` (host) or `/data/insights/top5/` (Compose).
- `POST /api/source-intakes` — submit an allowlisted media link for manual rights review and create a Founder’s Circle invite.
- `GET /api/source-intakes/{id}` / `POST /api/source-intakes/{id}/rights-review` — inspect a lead and record the operator's `cleared` or `rejected` decision with a short, non-sensitive basis.
- `POST /api/archive/discover` — search MusicBrainz and stage up to 10 artist candidates per selected archive genre (all five by default).
- `GET /api/archive/candidates` / `POST /api/archive/candidates/{id}/review` — inspect candidates and manually approve or reject them; approval creates an artist profile only.
- `GET /api/founders/{invite_id}` / `POST /api/founders/{invite_id}/rsvp` — view and answer the opt-in invitation.
- `GET /api/profile` — free-tier status and badge progress.

Mutating endpoints use `Authorization: Bearer <PULSE_API_TOKEN>`. Interactive API docs are available at `/docs`.
LIVEPULSE event delivery uses an in-process queue for the current single-Uvicorn-worker MVP. The 30-second polling fallback recovers current state after reconnects; multi-worker deployments will need a shared event broker.

## Tests and development

```powershell
py -m pip install -r requirements-dev.txt
py -m pytest -q
```

To run without Docker, configure `PULSE_API_TOKEN` and optionally `PULSE_USER_AREA`, then start `py -m uvicorn main:app --reload`. Local defaults store the database beside `main.py` and uploads in `uploads/`.

The root `Dockerfile` builds the API image. `n8n-stack/docker-compose.yml` remains a separate n8n/Ollama stack.
