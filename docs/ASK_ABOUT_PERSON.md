# Ask About a Person

Feature flag: `ASK_ABOUT_PERSON_ENABLED` (default **false**).

Buddy answers person questions inside the existing chat router. It does not add a second agent, change Daily Briefing, sync Gmail, or send mail.

## Flow

1. Briefing, selected-email, follow-up, count, and comparison questions keep their current routes.
2. When the flag is on and the question is about a person, chat calls `retrieve_person_context` and skips the inbox planner.
3. When the flag is off, that question uses the existing planner.

## Resolution

People are grouped from stored `From` headers for the connected Gmail account only. The same display name with two email addresses stays two people. Addresses merge only when `person_contact_aliases.verified` is true.

Migration (do not run automatically): `docs/migrations/2026-11-05_person_contact_aliases.sql`.

If that table is missing, addresses stay separate and a multi-match answer says alias linking is unavailable.

## Evidence

Incoming mail matches the selected From address. Outgoing mail is included only when it is already stored on one of those threads. There is no To/Cc column, and SENT sync is inbox-primary, so a sent-only thread can be missing. The answer says so instead of inventing it.

Counts, pending actions, deadlines, and follow-up status come from stored analyses and `briefing_recommendation_state`. OpenAI may phrase the answer from that evidence. A commitment that is not in the evidence is discarded.

## API

| Method | Path | Role |
|---|---|---|
| GET | `/db/person-context/status` | `{enabled}` |
| POST | `/chat` | Existing body. Person turns may add `candidates` and `person`. |

`person.draft_email_id` opens the existing draft endpoint. `person.recommendation_key` reuses follow-up draft and remind-tomorrow. Neither sends mail.

## Enable (manual)

1. Leave the flag false until verification.
2. Apply the alias migration only if verified aliases are required.
3. Set `ASK_ABOUT_PERSON_ENABLED=true` and recreate the backend. Rebuild the frontend image if it is not volume-mounted.
