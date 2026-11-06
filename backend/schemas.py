from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EmailAnalysis(BaseModel):
    summary: str = Field(
        ...,
        min_length=1,
        description="A short summary of the email"
    )

    category: Literal[
        "work", "personal", "finance", "other"
    ] = Field(
        ...,
        description="Category of the email"
    )

    importance: Literal[
        "low", "medium", "high"
    ] = Field(
        ...,
        description="Importance level of the email"
    )

    deadlines: list[str] = Field(
        default_factory=list,
        description="Deadlines mentioned in the email"
    )

    action_required: bool = Field(
        ...,
        description="Whether the email requires an action"
    )

    confidence: float = Field(
        ...,
        ge=0,
        le=1,
        description="Confidence score between 0 and 1"
    )


class AnalyzeRequest(BaseModel):
    message_id: str = Field(..., min_length=1)


class ChatSource(BaseModel):
    email_id: str
    subject: str | None = None
    sender: str | None = None
    gmail_thread_id: str | None = None
    gmail_message_id: str | None = None
    importance: str | None = None
    reason: str | None = None
    received_at: str | None = None


class ChatHistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)
    sources: list[ChatSource] | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _keep_history_short(cls, value: object) -> str:
        text = str(value or "").strip()
        if len(text) <= 4000:
            return text
        return text[:3997].rstrip() + "..."


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    email_id: str | None = None
    session_id: str | None = Field(default=None, max_length=128)
    history: list[ChatHistoryTurn] | None = Field(
        default=None,
        description="Optional prior turns (newest last); max 6 applied server-side",
    )
    selected_contact_id: str | None = Field(
        default=None,
        max_length=320,
        description="Canonical email chosen from a disambiguation card",
    )
    bound_contact_id: str | None = Field(
        default=None,
        max_length=320,
        description="Person bound in the current chat panel only",
    )
    candidate_offset: int = Field(default=0, ge=0, le=500)


class ChatDraft(BaseModel):
    id: str
    email_id: str
    body: str
    status: str


class PersonCandidate(BaseModel):
    id: str
    name: str
    email: str
    company: str | None = None
    last_interaction: str = ""
    last_topic: str = ""
    match_reason: str = ""
    context: str | None = None


class PersonContextCard(BaseModel):
    name: str
    email: str = ""
    addresses: list[str] = Field(default_factory=list)
    summary: str = ""
    last_interaction: str = ""
    pending_actions: list[str] = Field(default_factory=list)
    follow_up: str = ""
    emails: list[ChatSource] = Field(default_factory=list)
    sent_history_incomplete: bool = False
    draft_email_id: str | None = None
    recommendation_key: str | None = None
    alias_note: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource] = Field(default_factory=list)
    draft: ChatDraft | None = None
    freshness_note: str | None = None
    candidates: list[PersonCandidate] = Field(default_factory=list)
    candidate_total: int = 0
    candidate_offset: int = 0
    candidate_has_more: bool = False
    person: PersonContextCard | None = None


class InboxQueryPlan(BaseModel):
    """Structured retrieval filters for Ask My Inbox (no vector DB)."""

    sender_contains: str | None = Field(
        default=None,
        description="Substring to match in sender, or null",
    )
    subject_keywords: list[str] = Field(default_factory=list)
    body_keywords: list[str] = Field(default_factory=list)
    date_from_iso: str | None = Field(
        default=None,
        description="Inclusive local/ISO date YYYY-MM-DD if user specified a start",
    )
    date_to_iso: str | None = Field(
        default=None,
        description="Inclusive end date YYYY-MM-DD if specified",
    )
    importance: Literal["low", "medium", "high"] | None = None
    action_required: bool | None = None
    unread_only: bool = Field(
        default=False,
        description="True when the question asks about unread mail",
    )
    mailbox: Literal["inbox", "sent", "all"] = Field(
        default="inbox",
        description="inbox, sent, or all stored mail. Leave inbox unless the user was explicit.",
    )
    result_limit: int | None = Field(
        default=None,
        description="Requested list size such as top 5. Null when the user did not ask for a count of rows.",
    )
    rank_by: Literal["importance", "recency"] | None = Field(
        default=None,
        description="importance ranks by saved analysis. recency keeps newest first. Null for ordinary search.",
    )
    reply_need: Literal["confirmed", "outgoing"] | None = None
    deadline_only: bool = False
    newsletter_only: bool = False
    response_language: Literal["en", "hi", "hinglish"] = "en"
    question_kind: Literal[
        "focused",
        "count",
        "broad_summary",
        "comparison",
        "general",
        "draft_reply",
        "briefing",
    ] = Field(
        default="focused",
        description=(
            "focused: retrieve a few relevant emails; "
            "count: how many emails match; "
            "broad_summary: summarize a bounded set of matches; "
            "comparison: compare counts across two periods; "
            "general: not about the mailbox; "
            "draft_reply: generate a reply draft for one email; "
            "briefing: answer from the saved daily briefing"
        ),
    )
    use_recent_fallback: bool = Field(
        default=False,
        description="True when the question does not imply filters",
    )
