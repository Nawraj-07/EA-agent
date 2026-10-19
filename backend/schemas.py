from typing import Literal

from pydantic import BaseModel, Field


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


class ChatHistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    email_id: str | None = None
    session_id: str | None = Field(default=None, max_length=128)
    history: list[ChatHistoryTurn] | None = Field(
        default=None,
        description="Optional prior turns (newest last); max 6 applied server-side",
    )


class ChatSource(BaseModel):
    email_id: str
    subject: str | None = None
    sender: str | None = None
    gmail_thread_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource] = Field(default_factory=list)


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
    use_recent_fallback: bool = Field(
        default=False,
        description="True when the question does not imply filters",
    )
