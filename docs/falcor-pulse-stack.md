# FalCor Pulse — proof-of-concept stack

This diagram distinguishes the live host-run API from the configured-but-stopped Docker/Jupyter stack and the agentic components not yet connected to Pulse.

```mermaid
flowchart TB
    listener["Listener / artist"]
    browser["Pulse landing page<br/>SVG nodes · track controls · badges"]

    subgraph pulse["Pulse API · live on host Uvicorn; Docker Compose configured"]
        api["FastAPI + Uvicorn<br/>GET /api/pulse · artists · tracks<br/>listens · votes · profile"]
        score["HeatScore engine<br/>40% recent listens · 30% local votes<br/>30% freshness"]
        gains["Per-artist Heat gain hits<br/>+1 when a listen/vote raises score<br/>otherwise hold; never reset"]
        viz["SVG node mapping<br/>radius · heat color/glow · pulse rate"]
        sqlite[("SQLite<br/>artist scores + gain counters<br/>Host: messages.db · Compose: /data/pulse.db")]
        audio[("Authorized uploads<br/>Host: uploads/<br/>Compose: /data/uploads")]
        badges["Free-tier rules<br/>unlimited listens · optional vote prompt<br/>Listener · Scout · Pulse Setter"]
        api <--> sqlite
        api <--> audio
        api --> score
        score --> viz
        score --> gains --> sqlite
        score --> badges
    end

    subgraph notebook["Docker Compose configuration · currently stopped"]
        jupyter["JupyterLab<br/>token protected · localhost:8888"]
    end

    subgraph adjacent["Existing adjacent stack · n8n-stack/docker-compose.yml"]
        n8n["n8n<br/>localhost:5678"]
        ollama["Ollama<br/>localhost:11434<br/>model selection TBD"]
        n8n <-->|same n8n-network; workflow not loaded| ollama
    end

    secrets["Ignored .env<br/>API token · Jupyter token<br/>profile / area · badge thresholds"]

    listener --> browser
    browser -->|"GET / · GET /api/pulse"| api
    browser -->|"Bearer token for writes"| api
    api --> browser
    jupyter -->|"PULSE_API_URL=http://api:8000"| api
    secrets -.->|"runtime configuration"| api
    secrets -.->|"JUPYTER_TOKEN"| jupyter
    n8n -.->|"future authenticated API workflow"| api
```

## What is wired up

| Layer | MVP implementation |
|---|---|
| Experience | FastAPI-served landing page; LIVEPULSE™ pushes live SVG refreshes; 30-second polling fallback; authorized audio controls |
| API | FastAPI served by Uvicorn; public pulse summary and SSE events; bearer-token protected writes; profile, badges, listens, votes |
| HeatScore | 0–100: `40 × min(listens_24h / 50, 1) + 30 × min(local_votes_24h / 10, 1) + 30 × max(1 - days_since_signal / 30, 0)` |
| Heat gain hits | Each listen/local vote compares pre/post score; add one only on an increase. Persistent per-artist count holds on flat/lower checks; refreshes/uploads do not count |
| Node rendering | Radius `12 + 0.36 × score`; cyan → violet → pink color/glow; pulse period `4.5 - 0.034 × score` seconds |
| Free tier | Unlimited listens; prompt to vote beginning with listen 51 per UTC day; voting stays optional |
| Badges | Listener (10 listens), Scout (5 local votes), Pulse Setter (first HeatScore contribution); first two thresholds are configurable |
| Persistence | Current host run uses `messages.db` and `uploads/`; Compose uses a separate persistent `pulse_data` volume at `/data/pulse.db` and `/data/uploads` |
| Analysis | JupyterLab is configured to reach the API over Compose networking, but is not running while Docker Engine is stopped |

With no artists in the database, the landing page shows sample HeatScore nodes at 18, 55, and 90. Those are illustrative only; populated nodes always use the live API scores.
LIVEPULSE uses an in-process event queue for the single-worker MVP. The 30-second polling fallback recovers state after reconnects; horizontal scaling needs a shared event broker.

## Runtime comparison

- **Live now:** Uvicorn on `127.0.0.1:8000`, serving the landing page and SSE feed; the first artist is stored in the host `messages.db`.
- **Configured, not running:** root Docker Compose API and JupyterLab. Docker Engine is unavailable in the current session, so Compose's named volumes are not mounted.
- **Important when switching:** Compose uses `/data/pulse.db` in `pulse_data`, which is separate from host `messages.db`. The first artist and any host uploads need a deliberate migration before using the containerized database.

## Runtime and agentic variables

### Configured environment

| Variable | Purpose | Default / source |
|---|---|---|
| `PULSE_API_TOKEN` | Bearer token protecting writes and profile data | Random local secret in ignored `.env`; required by Compose |
| `JUPYTER_TOKEN` | JupyterLab login token | Separate random local secret in ignored `.env` |
| `PULSE_API_URL` | API URL visible inside the Jupyter container | `http://api:8000` |
| `PULSE_DB_FILE` | SQLite database location | `/data/pulse.db` in Compose |
| `PULSE_UPLOAD_DIR` | Authorized audio storage | `/data/uploads` in Compose |
| `PULSE_PROFILE_NAME` | Label shown for the local profile | `Local Listener` |
| `PULSE_PROFILE_ID` | Local profile identifier used for listen/vote totals | `local-listener` |
| `PULSE_USER_AREA` | Area used to decide whether an artist is votable | `Local`; manually configured, not geolocation-verified |
| `PULSE_BADGE_LISTENER_STREAMS` | Listener badge threshold | `10` |
| `PULSE_BADGE_SCOUT_VOTES` | Scout badge threshold | `5` |

The vote prompt threshold (`50`), upload maximum (`25 MiB`), allowed audio formats (MP3, WAV, OGG, FLAC, M4A, AAC), recent-listen window (24 hours), vote normalization cap (`10`), and freshness window (30 days) are currently code constants, not environment settings.

### Agentic layer: now vs. next

| Agentic concern | Current state | Next wiring step |
|---|---|---|
| Goal | Help listeners discover local artists and keep the pulse current | Define an agent task contract, such as summarizing notable local changes |
| Inputs / context | Recent listens, area-matched votes, artist age/activity, track metadata | Add explicit context limits and source attribution |
| Tools | Pulse HTTP API; Jupyter for local analysis | Give an agent a restricted API client; keep voting and publishing user-confirmed |
| Memory | SQLite stores MVP records; Docker persistence is configured, while the current host run uses `messages.db` | Define retention, event history, and per-user data boundaries |
| Model | Ollama container is configured in the separate n8n stack; no model is selected or loaded for Pulse | Choose a model and resource limits, then connect it through an approved workflow |
| Orchestration | n8n exists as a separate stack; no Pulse workflow or shared network is configured | Add a reviewed n8n workflow and authenticated connection to the Pulse API |
| Credentials | Pulse API and Jupyter use distinct `.env` tokens | Use separate least-privilege credentials for any future agent; never put secrets in prompts or notebooks |
| Guardrails | Licensed uploads are attested; 25 MiB cap; votes are optional and area-string matched | Add verified user accounts, authorization scopes, abuse controls, and auditable actions before public launch |

The current deployment is a single local profile and one shared API write token, not public multi-user authentication. The configured n8n/Ollama containers are separate and do not currently call the Pulse API.
