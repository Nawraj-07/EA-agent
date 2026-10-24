# Hardening notes (Task 6.3)

## Already in place

- Secrets loaded from backend `.env` only (`OPENAI_API_KEY`, Google, Supabase service role, `GMAIL_TOKEN_ENCRYPTION_KEY`)
- Gmail OAuth JSON is Fernet-encrypted in `gmail_oauth_credentials` (service_role only; no anon policies)
- OAuth state + PKCE via signed sessions
- Draft send uses atomic `pending → sending → sent` claim
- Uncertain Gmail send outcomes do not reset to `pending` (avoids double-send)
- Scheduler off by default; manual sync/briefing gated by `ENABLE_MANUAL_SYNC`
- CORS origins derived from `FRONTEND_URL` + optional `CORS_ORIGINS`
- `SESSION_HTTPS_ONLY` for production cookies
- API error responses avoid dumping raw exception strings to clients (prefer logs)

## Operator checklist

1. Rotate `SESSION_SECRET` if it was ever committed or shared  
2. Confirm `.gitignore` includes `.env` and `token.json`  
2b. Never log or commit `token.json`, Fernet keys, or OAuth ciphertext  
3. Use service role only on the server; never in React  
4. Run one scheduler process only  
5. Review Supabase RLS: enabled on tables; backend bypasses via service role  
6. After deploy, set Google redirect URI to HTTPS callback  

## Follow-ups (optional later)

- Key rotation for `GMAIL_TOKEN_ENCRYPTION_KEY` (`key_version` on `gmail_oauth_credentials`)
- Add auth for dashboard users (multi-user)
- Rate-limit `/chat`, `/emails/analyze`, `/drafts/*/approve`
- Structured logging + alerting on sync/send failures
