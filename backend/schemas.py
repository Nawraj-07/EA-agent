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



class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    email_id: str | None = None