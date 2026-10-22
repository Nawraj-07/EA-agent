# Deployment checklist (Task 6.4)

Use this when moving from local learning to a hosted environment.

## 1. Secrets and config

- [ ] Never commit `.env`, `token.json`, or service-role keys
- [ ] Set production values for:
  - `OPENAI_API_KEY`, `OPENAI_MODEL`
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`
  - `SESSION_SECRET` (long random; regenerate for prod)
  - `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL`
  - `FRONTEND_URL`, `BACKEND_URL`, `VITE_API_URL` (build-time for Vite)
- [ ] `SESSION_HTTPS_ONLY=true` behind HTTPS
- [ ] `CORS_ORIGINS` = your production frontend origin(s), comma-separated
- [ ] `ENABLE_SCHEDULER=true` on **exactly one** process (not every replica)
- [ ] Keep `ENABLE_MANUAL_SYNC=false` in production

## 2. Google OAuth (production)

- [ ] Create / update OAuth client for production
- [ ] Authorized redirect URI = `https://<your-api-host>/auth/google/callback`
- [ ] Consent screen: add production domains / test users as needed
- [ ] Re-consent so tokens include `gmail.readonly`, `gmail.send`, `openid`, `userinfo.email`, and `userinfo.profile` (needed for the Home profile photo)

## 3. Database

- [ ] Run [`docs/Schema.sql`](Schema.sql) (or migrations under `docs/migrations/`) on production Supabase
- [ ] Confirm RLS stays enabled; backend uses **service_role** only
- [ ] Confirm `drafts.status` allows `pending`, `sending`, `approved`, `rejected`, `sent`
- [ ] Confirm `daily_briefings` table exists

## 4. Backend host

Suggested: Railway, Render, Fly.io, or any Linux VM.

- [ ] Python 3.10+, install deps from venv / `requirements` if you add one
- [ ] Start with a **single worker** if the scheduler runs in-process:
  - Example: `uvicorn main:app --host 0.0.0.0 --port 8000`
- [ ] Persist or re-issue Gmail OAuth tokens (`token.json` is local-dev only; prefer encrypted DB storage later)
- [ ] Health check: `GET /health`

## 5. Frontend host

Suggested: Vercel, Netlify, or Cloudflare Pages (static Vite build).

- [ ] `npm run build` with `VITE_API_URL=https://<your-api-host>`
- [ ] Deploy `frontend/dist`
- [ ] Confirm CORS allows the frontend origin

## 6. Scheduler / briefing

- [ ] Gmail sync interval: `GMAIL_SYNC_INTERVAL_MINUTES` (default 15)
- [ ] Daily briefing cron (UTC): `BRIEFING_HOUR_UTC` / `BRIEFING_MINUTE_UTC` (default 08:00)
- [ ] Optional: run scheduler as a separate process instead of inside the API

## 7. Smoke test after deploy

1. Open frontend → load inbox  
2. Connect Gmail (OAuth)  
3. Analyze or wait for sync  
4. Chat about an email  
5. Generate draft → edit → approve/send to yourself  
6. Confirm daily briefing appears after job or manual `briefing-now` in staging  

## 8. Security reminders

- Service role key and OpenAI key: backend only  
- Do not put secrets in `VITE_*`  
- Prefer short-lived logs; never log tokens or authorization codes  
- Rate-limit public endpoints if you expose the API publicly  
