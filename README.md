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
SUPABASE_URL=
DATABASE_URL=
FRONTEND_URL=http://localhost:3000
BACKEND_URL=http://localhost:8000
VITE_API_URL=http://localhost:8000
ENABLE_SCHEDULER=false
GMAIL_SYNC_INTERVAL_MINUTES=15
```

Set `ENABLE_SCHEDULER=true` in your local `.env` to auto-sync Gmail every 15 minutes.
Run a one-shot sync for testing: `cd backend && python scheduler.py`

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
| Daily briefing | Cron at `BRIEFING_HOUR_UTC` (default 08:00 UTC); UI panel on inbox |
| Manual briefing | `ENABLE_MANUAL_SYNC=true` then `POST /internal/briefing-now` |
| Deploy | See [docs/DEPLOY.md](docs/DEPLOY.md) |
| Hardening | See [docs/HARDENING.md](docs/HARDENING.md) |

**DB:** Run `docs/migrations/2026-10-16_daily_briefings.sql` in Supabase if upgrading an existing project.

**Development Approach:** Understand each component, implement features independently, test functionality, and gradually introduce advanced capabilities.