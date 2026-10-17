
"""Unit tests for draft generation decision logic (Task 5.2)."""

from unittest.mock import MagicMock, patch

from draft_service import (
    NO_REPLY_TOKEN,
    generate_email_draft,
    is_no_reply_needed,
    looks_like_automated_no_reply,
)


PROJECT_UPDATE_EMAIL = {
    "id": "11111111-1111-1111-1111-111111111111",
    "subject": "Project Status Update Request",
    "sender": "manager@example.com",
    "snippet": "Please send a progress update on the project.",
    "body": (
        "Hi,\n\n"
        "Could you please send a project status update by end of week? "
        "I need the latest progress so we can plan next steps.\n\n"
        "Thanks,\nAlex"
    ),
    "email_analyses": [
        {
            "summary": "Manager requests a project progress update.",
            "category": "work",
            "importance": "high",
            "deadlines": ["end of week"],
            "action_required": True,
            "confidence": 0.92,
        }
    ],
}

PASSWORD_RESET_EMAIL = {
    "id": "22222222-2222-2222-2222-222222222222",
    "subject": "Reset Your Password",
    "sender": "noreply@mail.app.supabase.io",
    "snippet": "Follow this link to reset your password.",
    "body": (
        "Follow this link to reset the password for your project:\n"
        "https://supabase.com/dashboard/reset-password\n\n"
        "If you did not request this, you can safely ignore this email."
    ),
    "email_analyses": [
        {
            "summary": "Supabase password reset notification.",
            "category": "other",
            "importance": "medium",
            "deadlines": [],
            "action_required": False,
            "confidence": 0.95,
        }
    ],
}


def test_is_no_reply_needed_variants():
    assert is_no_reply_needed(None) is True
    assert is_no_reply_needed("") is True
    assert is_no_reply_needed("NO_REPLY_NEEDED") is True
    assert is_no_reply_needed("NO_REPLY_NEEDED.") is True
    assert is_no_reply_needed("  no_reply_needed  ") is True
    assert is_no_reply_needed("Hi,\n\nThanks for your email.") is False


def test_password_reset_detected_as_automated():
    assert looks_like_automated_no_reply(PASSWORD_RESET_EMAIL) is True


def test_project_update_not_detected_as_automated():
    assert looks_like_automated_no_reply(PROJECT_UPDATE_EMAIL) is False


def test_password_reset_returns_no_reply_without_llm():
    with patch("draft_service.ChatOpenAI") as mock_chat:
        result = generate_email_draft(PASSWORD_RESET_EMAIL)

    assert result == NO_REPLY_TOKEN
    mock_chat.assert_not_called()


def test_project_status_update_generates_draft():
    fake_reply = (
        "Hi Alex,\n\n"
        "Thanks for reaching out. I will review the latest project details "
        "and send you an accurate progress update shortly.\n\n"
        "Best regards"
    )

    mock_response = MagicMock()
    mock_response.content = fake_reply

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    with patch("draft_service.ChatOpenAI", return_value=mock_llm):
        result = generate_email_draft(PROJECT_UPDATE_EMAIL)

    assert is_no_reply_needed(result) is False
    assert "progress update" in result.lower() or "update" in result.lower()

    messages = mock_llm.invoke.call_args[0][0]
    human_content = messages[1].content
    assert "Action required: true" in human_content
    assert "Project Status Update Request" in human_content

    system_content = messages[0].content
    assert "Do NOT return NO_REPLY_NEEDED for work emails" in system_content


def test_llm_no_reply_token_still_respected_for_security_mail():
    security_email = {
        "subject": "Weekly digest",
        "sender": "updates@newsletter.example",
        "body": "Here is your automated weekly digest.",
        "email_analyses": [
            {
                "summary": "Automated newsletter digest.",
                "category": "other",
                "importance": "low",
                "action_required": False,
            }
        ],
    }

    mock_response = MagicMock()
    mock_response.content = "NO_REPLY_NEEDED."

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    with patch("draft_service.ChatOpenAI", return_value=mock_llm):
        result = generate_email_draft(security_email)

    assert is_no_reply_needed(result) is True


if __name__ == "__main__":
    test_is_no_reply_needed_variants()
    test_password_reset_detected_as_automated()
    test_project_update_not_detected_as_automated()
    test_password_reset_returns_no_reply_without_llm()
    test_project_status_update_generates_draft()
    test_llm_no_reply_token_still_respected_for_security_mail()
    print("All draft_service tests passed.")
