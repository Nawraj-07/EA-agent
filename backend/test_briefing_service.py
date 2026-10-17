"""Unit tests for daily briefing content helpers (Task 6.2)."""

from briefing_service import BriefingContent, generate_briefing_content


def test_empty_inbox_briefing_without_llm():
    content = generate_briefing_content([])
    assert isinstance(content, BriefingContent)
    assert content.headline == "Quiet inbox"
    assert content.notes
    assert content.highlights == []


if __name__ == "__main__":
    test_empty_inbox_briefing_without_llm()
    print("All briefing_service tests passed.")
