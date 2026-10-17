
"""Unit tests for Gmail reply helpers (no network / no real send)."""

from gmail_service import extract_email_address, format_reply_subject


def test_extract_email_address():
    assert extract_email_address("Alex Manager <alex@example.com>") == "alex@example.com"
    assert extract_email_address("alex@example.com") == "alex@example.com"
    assert extract_email_address("  <BOB@Example.COM>  ") == "bob@example.com"
    assert extract_email_address("not-an-email") is None
    assert extract_email_address("") is None
    assert extract_email_address(None) is None


def test_format_reply_subject():
    assert format_reply_subject("Project Status Update Request") == (
        "Re: Project Status Update Request"
    )
    assert format_reply_subject("Re: Already replied") == "Re: Already replied"
    assert format_reply_subject("re: lowercase") == "re: lowercase"
    assert format_reply_subject("") == "Re: (no subject)"
    assert format_reply_subject(None) == "Re: (no subject)"


if __name__ == "__main__":
    test_extract_email_address()
    test_format_reply_subject()
    print("All gmail_service helper tests passed.")
