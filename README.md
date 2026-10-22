# Gmail AI Executive Assistant

A personal AI-powered Gmail assistant that reads and analyzes emails, generates summaries, identifies important notices, extracts deadlines, and manages notifications through a centralized web dashboard.

## Tech Stack

- **Frontend:** React, Vite, TypeScript
- **Backend:** Python, FastAPI
- **AI Integration:** OpenAI API + LangChain
- **Database:** Supabase PostgreSQL
- **Email Integration:** Gmail API + Google OAuth 2.0
- **Automation:** APScheduler
- **Code Quality:** ESLint

## Project Structure

```text
EMAIL_AI/
├── frontend/           # React + Vite application
│   └── src/
├── backend/            # FastAPI backend
│   └── main.py
├── docs/
│   └── architecture.md
├── .env.example        # Environment variable template
├── .gitignore
└── README.md
```

## Getting Started

### 1. Start Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at `http://localhost:3000`.

### 2. Start Backend

```bash
cd backend
.venv\Scripts\Activate.ps1
uvicorn main:app --reload --port 8000
```

Backend runs at `http://localhost:8000`.

API documentation: `http://localhost:8000/docs`

## Environment Variables

Configure the required credentials in your local `.env` files.

```dotenv
OPENAI_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
ALLOWED_GMAIL_ACCOUNT=gaurang@rbhargavaassociates.in
SUPABASE_URL=
DATABASE_URL=
FRONTEND_URL=http://localhost:3000
BACKEND_URL=http://localhost:8000
VITE_API_URL=http://localhost:8000
ENABLE_SCHEDULER=false
GMAIL_SYNC_INTERVAL_MINUTES=15
GMAIL_SYNC_BATCH_SIZE=100
ENABLE_MANUAL_SYNC=false
```

Set `ENABLE_SCHEDULER=true` in your local `.env` to auto-sync Gmail every 15 minutes
(resumable INBOX backfill + History API incremental updates).
Run a one-shot sync: `cd backend && python scheduler.py`
Refresh Inbox in the UI calls `POST /db/inbox/sync` (requires `ENABLE_MANUAL_SYNC=true`).

Every sync pass covers gaps so inbox mail is not left out of Buddy while
backfill walks older pages:

- newest INBOX pages (`GMAIL_NEWEST_PAGES`, default 3)
- last two days (`in:inbox newer_than:2d`)
- unread INBOX (`in:inbox is:unread`)
- reconcile: compare the newest INBOX pages (`GMAIL_RECONCILE_PAGES`, default 10)
  to local rows and ingest any missing ids

Gmail itself is never modified or deleted. Mail older than two days that is
already read and beyond the newest ~1,000 INBOX messages waits for backfill.

**Inbox ordering:** Conversations are grouped by Gmail thread and sorted by
`internal_date` (Gmail metadata), not Supabase `created_at`. Default tab is
**Primary** (`INBOX` + `CATEGORY_PERSONAL`).

Never commit actual API keys, OAuth tokens, or `.env` files to Git.

**Scheduler note:** Enable the scheduler in only one process. Multiple Uvicorn workers/replicas with `ENABLE_SCHEDULER=true` can duplicate sync work.

## Application Workflow

1. User connects Gmail through Google OAuth 2.0.
2. FastAPI fetches emails using Gmail API.
3. LangChain integrates OpenAI to summarize and classify emails.
4. Supabase stores email metadata, summaries, and notifications.
5. React displays processed information in the dashboard.
6. APScheduler periodically checks for new emails.

## Development Roadmap

- **Phase 1:** Project setup, architecture, and frontend-backend connectivity.
- **Phase 2:** Google OAuth and Gmail API integration.
- **Phase 3:** OpenAI + LangChain email analysis.
- **Phase 4:** Supabase database and dashboard.
- **Phase 5:** Notifications, reminders, and AI-generated drafts.
- **Phase 6:** Background automation, testing, and deployment.

## Current Status

**Phases 1–6 complete (local learning build):**

- Gmail OAuth, fetch, AI analysis, Supabase storage
- React inbox with chat, draft edit/reject, approve-and-send
- APScheduler Gmail sync + daily briefing (`ENABLE_SCHEDULER=true`)
- Inbox refresh + 60s auto-refresh for newly synced mail
- Hardening notes + deploy checklist in `docs/`

## Phase 6 extras

| Feature | How |
|--------|-----|
| Gmail sync | `ENABLE_SCHEDULER=true` or `python scheduler.py` |
| Email summaries | Pending mail is analyzed newest-first every minute (`GMAIL_ANALYZE_PER_RUN`, default 100) |
| Profile chip | `GET /auth/me`; reconnect once at `/auth/google` for the Google photo |
| Refresh Inbox | `ENABLE_MANUAL_SYNC=true` then UI button / `POST /db/inbox/sync` |
| Conversations API | `GET /db/conversations?category=primary` |
| Daily briefing | Cron at `BRIEFING_HOUR_UTC` (default 08:00 UTC); UI panel on inbox |
| Manual briefing | `ENABLE_MANUAL_SYNC=true` then `POST /internal/briefing-now?date=YYYY-MM-DD` |
| Ask My Inbox | `POST /chat` with optional `history`; NL filters + `sources[]` |
| Deploy | See [docs/DEPLOY.md](docs/DEPLOY.md) |
| Hardening | See [docs/HARDENING.md](docs/HARDENING.md) |

**DB migrations (existing projects):** run in Supabase SQL Editor in order:
1. `docs/migrations/2026-10-16_daily_briefings.sql`
2. `docs/migrations/2026-10-19_gmail_sync_ordering.sql` (thread/labels/`internal_date`/sync state)
3. `docs/migrations/2026-10-19_briefing_account_history.sql` (account-scoped briefing history)
4. `docs/migrations/2026-10-19_briefing_date.sql` (`briefing_date` for per-day generate)
5. `docs/migrations/2026-10-19_drop_morning_agendas.sql` (if you previously created `morning_agendas`)

**Briefing history:** `GET /db/briefings/history` returns today + previous 7 calendar days
in `BRIEFING_TIMEZONE` (default `Asia/Kolkata`). No OpenAI calls on read. UI date strip
defaults to today. `POST /internal/briefing-now?date=YYYY-MM-DD` generates for that day.

**Ask My Inbox:** Without `email_id`, the server plans SQL filters from the question
(`CHAT_RETRIEVAL_LIMIT`, default 15). Responses include clickable `sources`.

**Development Approach:** Understand each component, implement features independently, test functionality, and gradually introduce advanced capabilities.

## Docker Setup

Buddy AI is containerized using **Docker and Docker Compose**, with separate containers for the React frontend (port `3000`) and FastAPI backend (port `8000`). The backend connects to external services such as Supabase, Gmail API, and OpenAI.

**Prerequisites:** Docker Desktop, configured `.env` credentials, and an authorized Gmail OAuth token (`backend/token.json`).

**Build and run:**

```bash
docker compose build
docker compose up -d
```

**Useful commands:**

```bash
docker compose ps          # Check container status
docker compose logs -f     # View application logs
docker compose stop        # Stop containers
docker compose start       # Restart containers
docker compose down        # Remove containers and network
```

**Application URLs:**
- Frontend: http://localhost:3000
- Backend: http://localhost:8000
- API Docs: http://localhost:8000/docs

**Note:** Supabase and Gmail remain external services. Keep `.env` and `token.json` private, and avoid `docker compose down -v` to prevent deleting Docker volumes.