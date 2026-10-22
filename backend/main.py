# ========== SECTION 1: IMPORTS ==========
import json
import logging
import os
import secrets
import base64
import re
from contextlib import asynccontextmanager
from langchain_openai import ChatOpenAI

from typing import Literal
from pydantic import BaseModel, Field
from db import (
    get_draft,
    update_draft,
    get_draft_with_email,
    claim_draft_for_sending,
    release_draft_claim,
    mark_draft_sent,
)
from uuid import UUID
from db import get_stored_email, save_draft
from draft_service import generate_email_draft, is_no_reply_needed
from gmail_service import extract_email_address, send_gmail_reply
from schemas import ChatRequest, AnalyzeRequest
from chat_service import retrieve_emails_for_question, run_chat
from db import (
    list_conversations,
    list_stored_emails,
    list_thread_messages,
    get_stored_email,
    save_email_and_analysis,
    search_emails_for_chat,
)
from ai_service import analyze_email_content
from gmail_safety import (
    GmailAccessDenied,
    assert_allowed_gmail_account,
    assert_credentials_safe,
    assert_gmail_scopes_safe,
    get_allowed_gmail_account,
)
from gmail_sync import get_connected_profile, get_profile_email
from html import unescape
from pathlib import Path
from html.parser import HTMLParser
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from starlette.middleware.sessions import SessionMiddleware

# ========== SECTION 2: ENVIRONMENT VARIABLES & PATHS ==========
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')
load_dotenv(BASE_DIR.parent / '.env', override=False)

GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:8000/auth/google/callback')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:3003')
SESSION_SECRET = os.getenv('SESSION_SECRET')
SESSION_HTTPS_ONLY = os.getenv('SESSION_HTTPS_ONLY', 'false').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}


def _cors_allow_origins() -> list[str]:
    """FRONTEND_URL plus optional comma-separated CORS_ORIGINS (Task 6.3)."""
    origins: list[str] = []
    primary = (FRONTEND_URL or '').rstrip('/')
    if primary:
        origins.append(primary)
    extra = os.getenv('CORS_ORIGINS', '')
    for part in extra.split(','):
        cleaned = part.strip().rstrip('/')
        if cleaned and cleaned not in origins:
            origins.append(cleaned)
    if 'http://localhost:3000' not in origins and primary.startswith('http://localhost'):
        origins.append('http://localhost:3003')
    return origins or ['http://localhost:3003']


if not all((GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SESSION_SECRET)):
    raise RuntimeError('Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and SESSION_SECRET in .env')

# Read existing mail + send new replies only. Never request modify/delete.
# userinfo is read-only identity for the Home profile chip.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
TOKEN_PATH = BASE_DIR / 'token.json'
logger = logging.getLogger(__name__)
_verified_gmail_account: str | None = None


# ========== SECTION 3: FASTAPI LIFESPAN, CORS & SESSION SETUP ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Start/stop the Gmail sync scheduler with the API process.

    Production note: enable the scheduler in only one process. Multiple
    Uvicorn workers or replicas with ENABLE_SCHEDULER=true can duplicate sync
    work unless you add distributed locking or a dedicated scheduler worker.
    """
    # Late import keeps module import light and avoids circular imports.
    from scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_methods=['*'],
    allow_headers=['*'],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site='lax',
    https_only=SESSION_HTTPS_ONLY,
)


# ========== SECTION 4: GOOGLE OAUTH CONFIGURATION ==========
def create_google_flow() -> Flow:
    flow = Flow.from_client_config(
        {
            'web': {
                'client_id': GOOGLE_CLIENT_ID,
                'client_secret': GOOGLE_CLIENT_SECRET,
                'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    return flow


# ========== SECTION 5A: SAVE TOKENS ==========
def save_credentials(credentials: Credentials) -> None:
    # For local development only: this file contains sensitive OAuth secrets.
    TOKEN_PATH.write_text(credentials.to_json(), encoding='utf-8')


# ========== SECTION 5B: LOAD & REFRESH TOKENS ==========
def load_gmail_credentials() -> Credentials:
    if not TOKEN_PATH.is_file():
        raise HTTPException(status_code=401, detail='Gmail not connected. Visit /auth/google first.')

    try:
        token_data = json.loads(TOKEN_PATH.read_text(encoding='utf-8'))
        stored_scopes = token_data.get('scopes') or SCOPES
        assert_gmail_scopes_safe(stored_scopes)
        # Refresh with granted scopes only. Extra identity scopes require reconnect.
        creds = Credentials.from_authorized_user_info(
            token_data,
            scopes=stored_scopes,
        )
        assert_credentials_safe(creds)
    except GmailAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning('Could not load stored Gmail credentials: %s', type(exc).__name__)
        raise HTTPException(status_code=401, detail='Saved Gmail credentials are invalid. Reconnect at /auth/google.') from exc

    if not creds.valid:
        if not creds.refresh_token:
            raise HTTPException(status_code=401, detail='Gmail session expired. Reconnect at /auth/google.')
        try:
            creds.refresh(GoogleRequest())
            save_credentials(creds)
        except (RefreshError, OSError, ValueError) as exc:
            logger.warning('Could not refresh Gmail credentials: %s', type(exc).__name__)
            raise HTTPException(status_code=401, detail='Gmail token refresh failed. Reconnect at /auth/google.') from exc

    global _verified_gmail_account
    allowed = get_allowed_gmail_account()
    if _verified_gmail_account != allowed:
        try:
            _verified_gmail_account = assert_allowed_gmail_account(
                get_profile_email(creds)
            )
        except GmailAccessDenied as exc:
            _verified_gmail_account = None
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    return creds


# ========== SECTION 6: HEALTH CHECK API ==========
@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/auth/me')
def auth_me():
    """Connected Gmail identity for the profile chip. Read-only."""
    credentials = load_gmail_credentials()
    return get_connected_profile(credentials)


# ========== SECTION 7: GOOGLE LOGIN API (TASK 2.3) ==========
@app.get('/auth/google')
def google_login(request: Request):
    flow = create_google_flow()
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='false',
    )
    request.session['oauth_state'] = state
    request.session['code_verifier'] = flow.code_verifier
    return RedirectResponse(authorization_url)


# ========== SECTION 8: OAUTH CALLBACK (TASK 2.4) ==========
@app.get('/auth/google/callback')
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        raise HTTPException(status_code=400, detail='Google authorization was denied or failed. Start again at /auth/google.')
    if not code or not state:
        raise HTTPException(status_code=400, detail='Missing authorization code or state. Start at /auth/google.')

    saved_state = request.session.pop('oauth_state', None)
    code_verifier = request.session.pop('code_verifier', None)
    if not saved_state or not secrets.compare_digest(state, saved_state):
        raise HTTPException(status_code=400, detail='Invalid OAuth state. Start again at /auth/google.')
    if not code_verifier:
        raise HTTPException(status_code=400, detail='Missing PKCE code verifier. Start again at /auth/google.')

    try:
        flow = create_google_flow()
        flow.code_verifier = code_verifier
        flow.fetch_token(code=code)
        credentials = flow.credentials
        assert_credentials_safe(credentials)
        connected = get_profile_email(credentials)
        assert_allowed_gmail_account(connected)
        save_credentials(credentials)
    except GmailAccessDenied as exc:
        logger.warning('OAuth rejected: unauthorized Gmail account or scopes')
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        # Avoid logging exception details: OAuth exceptions can contain sensitive data.
        logger.exception('Google OAuth callback failed')
        raise HTTPException(status_code=400, detail='OAuth token exchange or storage failed. Check backend logs.') from exc

    return RedirectResponse(f'{FRONTEND_URL.rstrip("/")}?gmail=connected')


# ========== SECTION 9: GMAIL MESSAGE LIST API (TASK 2.5) ==========
@app.get('/emails')
def list_emails():
    creds = load_gmail_credentials()
    try:
        service = build('gmail', 'v1', credentials=creds)
        result = service.users().messages().list(userId='me', maxResults=10).execute()
        return result
    except HttpError as exc:
        logger.warning('Gmail API request failed with status %s', exc.resp.status)
        raise HTTPException(status_code=502, detail='Gmail API request failed. Check backend logs.') from exc


# ========== SECTION 10: HTML TO TEXT CONVERTER  ==========

class HTMLTextExtractor(HTMLParser):

    def __init__(self):
        super().__init__()
        self.text = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip_depth += 1
        elif self.skip_depth == 0 and tag in ("br", "p", "div", "li"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.skip_depth == 0:
            self.text.append(data)

# ========== SECTION 11A: DECODE GMAIL BODY ==========

def decode_base64_body(data):
    if not data:
        return ""

    padding = "=" * (-len(data) % 4)

    decoded = base64.urlsafe_b64decode(data + padding)

    return decoded.decode("utf-8", errors="replace")

# ========== SECTION 11B: EXTRACT EMAIL BODY ==========

def extract_email_body(payload):
    plain_parts = []
    html_parts = []

    def walk(part):
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")

        if data:
            if mime_type == "text/plain":
                plain_parts.append(decode_base64_body(data))

            elif mime_type == "text/html":
                html_parts.append(decode_base64_body(data))

        for child in part.get("parts", []):
            walk(child)

    walk(payload)

    if plain_parts:
        return "\n".join(plain_parts).strip()

    if html_parts:
        parser = HTMLTextExtractor()
        parser.feed("\n".join(html_parts))
        return unescape("".join(parser.text)).strip()

    return ""

# ========== SECTION 12A: SHARED EMAIL FETCH HELPER ==========

def fetch_email_by_id(message_id: str) -> dict:
    creds = load_gmail_credentials()

    try:
        service = build("gmail", "v1", credentials=creds)

        message = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full"
            )
            .execute()
        )

    except HttpError as exc:
        if exc.resp.status == 404:
            raise HTTPException(
                status_code=404,
                detail="Email not found"
            ) from exc

        logger.warning(
            "Gmail message fetch failed with status %s",
            exc.resp.status
        )

        raise HTTPException(
            status_code=502,
            detail="Unable to fetch email from Gmail"
        ) from exc

    except Exception as exc:
        logger.exception("Unexpected Gmail fetch failure")

        raise HTTPException(
            status_code=502,
            detail="Gmail service is temporarily unavailable"
        ) from exc

    payload = message.get("payload", {})

    headers = {
        header["name"].lower(): header["value"]
        for header in payload.get("headers", [])
    }

    body = extract_email_body(payload)

    return {
        "id": message.get("id"),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
        "snippet": message.get("snippet", ""),
        "body": body
    }

# ========== SECTION 12B: GET FULL EMAIL ==========

@app.get("/emails/{message_id}")
def get_email(message_id: str):
    return fetch_email_by_id(message_id)


# ========== SECTION 13: OPENAI CONNECTIVITY ==========

@app.get("/ai/ping")
def ai_ping():
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="OPENAI_API_KEY is missing in .env"
        )

    llm = ChatOpenAI(
        model=model,
        api_key=api_key
    )

    result = llm.invoke("Reply with exactly: pong")

    return {"reply": result.content}




# ========== SECTION 14: EMAIL AI ANALYSIS (TASK 4.3) ==========

@app.post("/emails/analyze")
def analyze_email(payload: AnalyzeRequest):

    email = fetch_email_by_id(payload.message_id)

    try:
        analysis = analyze_email_content(
            subject=email["subject"],
            sender=email["from"],
            body=email["body"]
        )

    except Exception as exc:
        logger.error(
            "Email analysis failed: %s",
            type(exc).__name__
        )

        raise HTTPException(
            status_code=502,
            detail="AI email analysis failed. Please try again."
        ) from exc

    analysis_data = analysis.model_dump()

    try:
        save_email_and_analysis(email, analysis_data)

    except Exception as exc:
        logger.exception("Supabase save failed")

        raise HTTPException(
            status_code=502,
            detail="Email analyzed, but saving to database failed."
        ) from exc

    return {
        "email": email,
        "analysis": analysis_data
    }

# ========== SECTION 15: DATABASE READ APIS (TASK 4.4) ==========

def _connected_gmail_account() -> str | None:
    """Account used to scope DB reads.

    Prefer the live Gmail profile. If the token cannot refresh, still serve
    local inbox/briefings for the allowlisted mailbox.
    """
    try:
        return get_profile_email(load_gmail_credentials())
    except Exception:
        allowed = get_allowed_gmail_account()
        logger.warning(
            "Live Gmail unavailable; reading local data for %s",
            allowed,
        )
        return allowed or None


@app.get("/db/emails")
def get_saved_emails(
    limit: int = 20,
    cursor_internal_date: int | None = None,
):
    try:
        account = _connected_gmail_account()
        return list_stored_emails(
            limit=max(1, min(limit, 100)),
            gmail_account_email=account,
            in_inbox_only=True,
            cursor_internal_date=cursor_internal_date,
        )

    except Exception as exc:
        logger.exception("Failed to load stored emails")
        raise HTTPException(
            status_code=502,
            detail="Unable to fetch saved emails."
        ) from exc


def _parse_optional_iso_date(value: str | None, *, field: str) -> str | None:
    from datetime import date

    if value is None or value == "":
        return None
    text = value.strip()
    try:
        date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field}; expected YYYY-MM-DD.",
        ) from exc
    return text[:10]


@app.get("/db/conversations")
def get_conversations(
    category: str = "primary",
    limit: int = 20,
    cursor_internal_date: int | None = None,
    cursor_thread_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Gmail-style conversation list (newest thread activity first).

    Default category=primary (INBOX + CATEGORY_PERSONAL).
    Other values: promotions, social, inbox (all INBOX).

    Optional date_from / date_to (YYYY-MM-DD) filter threads with message
    activity in that inclusive local-calendar range (BRIEFING_TIMEZONE).
    """
    allowed = {"primary", "promotions", "social", "inbox"}
    if category not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid category. Use one of: {', '.join(sorted(allowed))}",
        )

    date_from_iso = _parse_optional_iso_date(date_from, field="date_from")
    date_to_iso = _parse_optional_iso_date(date_to, field="date_to")

    account = _connected_gmail_account()
    if not account:
        raise HTTPException(
            status_code=401,
            detail="Gmail not connected. Complete OAuth before loading inbox.",
        )

    try:
        return list_conversations(
            gmail_account_email=account,
            category=category,
            limit=max(1, min(limit, 50)),
            cursor_internal_date=cursor_internal_date,
            cursor_thread_id=cursor_thread_id,
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
        )
    except Exception as exc:
        logger.exception("Failed to load conversations")
        raise HTTPException(
            status_code=502,
            detail="Unable to fetch conversations.",
        ) from exc


@app.get("/db/conversations/{thread_id}")
def get_conversation_thread(thread_id: str):
    account = _connected_gmail_account()
    if not account:
        raise HTTPException(
            status_code=401,
            detail="Gmail not connected.",
        )

    try:
        messages = list_thread_messages(
            gmail_account_email=account,
            thread_id=thread_id,
        )
    except Exception as exc:
        logger.exception("Failed to load thread")
        raise HTTPException(
            status_code=502,
            detail="Unable to fetch conversation thread.",
        ) from exc

    if not messages:
        raise HTTPException(status_code=404, detail="Thread not found.")

    return {"thread_id": thread_id, "messages": messages}


@app.post("/db/inbox/sync")
def sync_inbox_now():
    """
    Trigger one Gmail sync cycle then return stats (frontend Refresh Inbox).

    Gated by ENABLE_MANUAL_SYNC=true (same as /internal/sync-now).
    """
    enabled = os.getenv("ENABLE_MANUAL_SYNC", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Manual inbox sync is disabled. Set ENABLE_MANUAL_SYNC=true "
                "in .env for local testing."
            ),
        )

    from scheduler import run_gmail_sync_once

    try:
        stats = run_gmail_sync_once()
    except Exception as exc:
        logger.exception("Manual inbox sync failed")
        raise HTTPException(
            status_code=502,
            detail="Inbox sync failed. Check backend logs.",
        ) from exc

    return {"ok": True, "stats": stats}


@app.get("/db/emails/{email_id}")
def get_saved_email(email_id: str):
    try:
        email = get_stored_email(email_id)

    except Exception as exc:
        logger.exception("Failed to load stored email")
        raise HTTPException(
            status_code=502,
            detail="Unable to fetch saved email."
        ) from exc

    if email is None:
        raise HTTPException(
            status_code=404,
            detail="Email not found."
        )

    return email


@app.post("/chat")
def chat_with_emails(payload: ChatRequest):
    """
    Ask My Inbox. Backward compatible: always returns {answer}.
    Extended: optional history, NL retrieval filters, sources[].
    """
    account = _connected_gmail_account()

    try:
        if payload.email_id:
            try:
                UUID(payload.email_id)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid email UUID."
                )

            email = get_stored_email(payload.email_id)
            if email is None:
                raise HTTPException(
                    status_code=404,
                    detail="Email not found."
                )
            emails = [email]
        else:
            if not account:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Gmail not connected. Complete OAuth before "
                        "asking the inbox."
                    ),
                )
            emails = retrieve_emails_for_question(
                payload.question,
                gmail_account_email=account,
                email_id=None,
                get_stored_email=get_stored_email,
                search_emails_for_chat=search_emails_for_chat,
                list_stored_emails=list_stored_emails,
            )

    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to load chat email context")
        raise HTTPException(
            status_code=502,
            detail="Unable to load email context."
        )

    try:
        return run_chat(
            question=payload.question,
            emails=emails,
            history=payload.history,
        )
    except Exception:
        logger.exception("AI chat failed")
        raise HTTPException(
            status_code=502,
            detail="Unable to generate an AI response."
        )

# ========== SECTION 16: DRAFT GENERATION (TASK 4.5) ==========
@app.post("/emails/{email_id}/draft")
def create_email_draft(email_id: str):
    try:
        UUID(email_id)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid email UUID."
        )

    try:
        email = get_stored_email(email_id)
    except Exception:
        logger.exception("Failed to load email for draft")
        raise HTTPException(
            status_code=502,
            detail="Unable to load email from database."
        )

    if email is None:
        raise HTTPException(
            status_code=404,
            detail="Email not found."
        )

    try:
        draft_body = generate_email_draft(email)
    except Exception:
        logger.exception("Draft generation failed")
        raise HTTPException(
            status_code=502,
            detail="Unable to generate draft."
        )

    if is_no_reply_needed(draft_body):
        raise HTTPException(
            status_code=422,
            detail="This email does not require a reply."
        )

    try:
        draft = save_draft(email_id, draft_body)
    except Exception:
        logger.exception("Failed to save draft")
        raise HTTPException(
            status_code=502,
            detail="Draft generated but could not be saved."
        )

    return {
        "id": draft["id"],
        "email_id": draft["email_id"],
        "body": draft["body"],
        "status": draft["status"],
    }

# ========== SECTION 17: DRAFT MANAGEMENT ==========
class DraftUpdateRequest(BaseModel):
    body: str | None = Field(default=None, min_length=1)
    status: Literal["approved", "rejected"] | None = None


@app.patch("/drafts/{draft_id}")
def patch_draft(draft_id: UUID, payload: DraftUpdateRequest):
    draft_id = str(draft_id)

    if payload.body is None and payload.status is None:
        raise HTTPException(
            status_code=422,
            detail="Provide body or status to update."
        )

    if payload.body is not None and not payload.body.strip():
        raise HTTPException(
            status_code=422,
            detail="Draft body cannot be empty."
        )

    try:
        draft = get_draft(draft_id)

        if draft is None:
            raise HTTPException(
                status_code=404,
                detail="Draft not found."
            )

        if draft["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail="Only pending drafts can be modified."
            )

        updates = {}

        if payload.body is not None:
            updates["body"] = payload.body

        if payload.status is not None:
            updates["status"] = payload.status

        from datetime import datetime, timezone

        updates["updated_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        updated = update_draft(draft_id, updates)

        if updated is None:
            raise HTTPException(
                status_code=409,
                detail="Draft is no longer pending."
            )

        return updated

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("Failed to update draft")

        raise HTTPException(
            status_code=502,
            detail="Unable to update draft."
        ) from exc


# ========== SECTION 18: APPROVE AND SEND DRAFT (TASK 5.4) ==========
@app.post("/drafts/{draft_id}/approve")
def approve_and_send_draft(draft_id: UUID):
    """
    Approve a pending draft and send it via Gmail.

    Flow: pending -> sending (atomic claim) -> Gmail send -> sent.
    Does not send on generate/edit. Uncertain Gmail outcomes leave status=sending.
    """
    draft_id_str = str(draft_id)

    try:
        record = get_draft_with_email(draft_id_str)
    except Exception as exc:
        logger.exception("Failed to load draft for approve/send")
        raise HTTPException(
            status_code=502,
            detail="Unable to load draft from database.",
        ) from exc

    if record is None:
        raise HTTPException(status_code=404, detail="Draft not found.")

    status = (record.get("status") or "").lower()
    email = record.get("emails")

    if isinstance(email, list):
        email = email[0] if email else None

    if status == "rejected":
        raise HTTPException(
            status_code=409,
            detail="Rejected drafts cannot be approved or sent.",
        )

    if status == "sent":
        raise HTTPException(
            status_code=409,
            detail="This draft was already sent.",
        )

    if status == "sending":
        raise HTTPException(
            status_code=409,
            detail=(
                "This draft is already being sent or may have an uncertain "
                "send status. Check Gmail before retrying."
            ),
        )

    if status == "approved":
        raise HTTPException(
            status_code=409,
            detail=(
                "This draft is marked approved but was not sent through the "
                "approve-and-send endpoint. Use a pending draft."
            ),
        )

    if status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Only pending drafts can be approved and sent.",
        )

    if not email:
        raise HTTPException(
            status_code=422,
            detail="Draft has no linked email to reply to.",
        )

    body = (record.get("body") or "").strip()
    if not body:
        raise HTTPException(
            status_code=422,
            detail="Draft body cannot be empty.",
        )

    recipient = extract_email_address(email.get("sender"))
    if not recipient:
        raise HTTPException(
            status_code=422,
            detail="Original sender does not contain a valid email address.",
        )

    try:
        claimed = claim_draft_for_sending(draft_id_str)
    except Exception as exc:
        # Common causes: status check constraint missing 'sending', or missing columns.
        logger.exception("Failed to claim draft for sending")
        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to claim draft for sending. "
                "Check drafts.status allows 'sending' and backend logs."
            ),
        ) from exc

    if claimed is None:
        raise HTTPException(
            status_code=409,
            detail="Draft is no longer pending (possible concurrent approval).",
        )

    send_confirmed = False

    try:
        creds = load_gmail_credentials()
        sent_message_id = send_gmail_reply(
            creds,
            to_email=recipient,
            subject=email.get("subject") or "",
            body=body,
            original_gmail_message_id=email.get("gmail_message_id"),
        )
        send_confirmed = True
    except HTTPException:
        # Auth errors from load_gmail_credentials — definite failure, release claim.
        release_draft_claim(draft_id_str)
        raise
    except ValueError as exc:
        release_draft_claim(draft_id_str)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HttpError as exc:
        status_code = getattr(exc.resp, "status", None)
        logger.warning(
            "Gmail send failed for draft %s with status %s",
            draft_id_str,
            status_code,
        )

        # Client errors are treated as definite failures (message not sent).
        if status_code is not None and 400 <= int(status_code) < 500:
            release_draft_claim(draft_id_str)
            raise HTTPException(
                status_code=502,
                detail="Gmail rejected the send request. Draft returned to pending.",
            ) from exc

        # 5xx / unknown: do not reset to pending (send may have succeeded).
        raise HTTPException(
            status_code=502,
            detail=(
                "Gmail send outcome is uncertain. Draft remains in 'sending' "
                "status — verify Sent mail in Gmail before retrying."
            ),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error while sending draft %s", draft_id_str)

        if send_confirmed:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Draft may have been sent but could not be finalized. "
                    "Status remains 'sending' — check Gmail before retrying."
                ),
            ) from exc

        # Failure before confirmed send — safe to release.
        release_draft_claim(draft_id_str)
        raise HTTPException(
            status_code=502,
            detail="Unable to send draft via Gmail.",
        ) from exc

    try:
        updated = mark_draft_sent(draft_id_str, sent_message_id)
    except Exception as exc:
        logger.exception(
            "Gmail send succeeded for draft %s but DB update failed",
            draft_id_str,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Email was sent, but the draft status could not be updated. "
                "Status may remain 'sending'. Do not retry without checking Gmail."
            ),
        ) from exc

    if updated is None:
        raise HTTPException(
            status_code=502,
            detail=(
                "Email was sent, but the draft could not be marked as sent. "
                "Do not retry without checking Gmail."
            ),
        )

    return {
        "id": updated["id"],
        "email_id": updated["email_id"],
        "body": updated["body"],
        "status": updated["status"],
        "gmail_sent_message_id": updated.get("gmail_sent_message_id")
        or sent_message_id,
        "recipient": recipient,
    }


# ========== SECTION 19: MANUAL GMAIL SYNC (TASK 6.1) ==========
@app.post("/internal/sync-now")
def trigger_gmail_sync_now():
    """
    Run one Gmail sync immediately (local/testing helper).

    Disabled unless ENABLE_MANUAL_SYNC=true. Prefer `python scheduler.py`
    from the backend folder when possible.
    """
    enabled = os.getenv("ENABLE_MANUAL_SYNC", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Manual sync is disabled. Set ENABLE_MANUAL_SYNC=true "
                "in .env for local testing, or run: python scheduler.py"
            ),
        )

    from scheduler import run_gmail_sync_once

    try:
        stats = run_gmail_sync_once()
    except Exception as exc:
        logger.exception("Manual Gmail sync failed")
        raise HTTPException(
            status_code=502,
            detail="Manual Gmail sync failed. Check backend logs.",
        ) from exc

    return {"ok": True, "stats": stats}


# ========== SECTION 20: DAILY BRIEFINGS (TASK 6.2) ==========
def _briefing_table_missing(exc: Exception) -> bool:
    message = str(exc)
    return "daily_briefings" in message or "PGRST205" in message


@app.get("/db/briefings/latest")
def get_latest_daily_briefing():
    """
    Return the most recent stored daily briefing for the connected account.

    Distinguishes:
    - 200 + briefing object → row exists
    - 200 + briefing null + status=empty → table OK, no rows yet
    - 503 → migration missing
    - 502 → unexpected DB/API failure
    """
    account = _connected_gmail_account()
    if not account:
        raise HTTPException(
            status_code=401,
            detail=(
                "Gmail not connected. Complete OAuth before loading briefings."
            ),
        )

    try:
        from db import get_latest_briefing

        briefing = get_latest_briefing(gmail_account_email=account)
    except Exception as exc:
        if _briefing_table_missing(exc):
            logger.warning(
                "daily_briefings table missing; run docs/migrations/"
                "2026-10-16_daily_briefings.sql in Supabase"
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "daily_briefings table is missing. "
                    "Run docs/migrations/2026-10-16_daily_briefings.sql "
                    "in the Supabase SQL Editor, then refresh."
                ),
            ) from exc
        logger.exception("Failed to load latest briefing (status=api_failure)")
        raise HTTPException(
            status_code=502,
            detail="Unable to fetch daily briefing.",
        ) from exc

    if briefing is None:
        logger.info("GET /db/briefings/latest: no rows (status=empty)")
        return {
            "briefing": None,
            "status": "empty",
            "message": (
                "No briefing has been generated yet. "
                "Click Generate briefing or wait for the daily scheduler job."
            ),
        }

    logger.info(
        "GET /db/briefings/latest: found id=%s account=%s",
        briefing.get("id"),
        account,
    )
    return {"briefing": briefing, "status": "ok"}


@app.get("/db/briefings/history")
def get_briefing_history():
    """
    Today + previous 7 calendar days of saved briefings (no OpenAI).

    Days without a briefing still appear with has_briefing=false.
    Per day, the latest successful (saved) row by created_at wins.
    """
    account = _connected_gmail_account()
    if not account:
        raise HTTPException(
            status_code=401,
            detail=(
                "Gmail not connected. Complete OAuth before loading briefings."
            ),
        )

    try:
        from briefing_service import get_briefing_history_for_account

        payload = get_briefing_history_for_account(account)
    except Exception as exc:
        if _briefing_table_missing(exc):
            raise HTTPException(
                status_code=503,
                detail=(
                    "daily_briefings table is missing or outdated. "
                    "Run docs/migrations/2026-10-16_daily_briefings.sql and "
                    "docs/migrations/2026-10-19_briefing_account_history.sql."
                ),
            ) from exc
        logger.exception("Failed to load briefing history")
        raise HTTPException(
            status_code=502,
            detail="Unable to fetch briefing history.",
        ) from exc

    return payload


@app.post("/internal/briefing-now")
def trigger_daily_briefing_now(date: str | None = None):
    """
    Generate one daily briefing immediately (local/testing helper).

    Optional query `date=YYYY-MM-DD` scopes the briefing to that calendar day
    (must be today or within the previous 7 days in BRIEFING_TIMEZONE).
    Omit date to generate for today.

    Disabled unless ENABLE_MANUAL_SYNC=true (same gate as sync-now).
    """
    enabled = os.getenv("ENABLE_MANUAL_SYNC", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Manual briefing is disabled. Set ENABLE_MANUAL_SYNC=true "
                "in .env for local testing."
            ),
        )

    from scheduler import run_daily_briefing_once

    try:
        stats = run_daily_briefing_once(target_date=date)
    except Exception as exc:
        logger.exception("Manual daily briefing failed (status=generation_failed)")
        raise HTTPException(
            status_code=502,
            detail="Manual daily briefing failed. Check backend logs.",
        ) from exc

    status = (stats or {}).get("status")
    if status == "invalid_date":
        raise HTTPException(
            status_code=422,
            detail=(stats or {}).get("error") or "Invalid briefing date.",
        )
    if status in {"load_failed", "generation_failed", "save_failed"}:
        logger.error("Manual briefing incomplete stats=%s", stats)
        raise HTTPException(
            status_code=502,
            detail=(
                f"Briefing generation did not save (status={status}). "
                "Check backend logs."
            ),
        )

    return {"ok": True, "stats": stats}