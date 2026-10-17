from ai_service import analyze_email_content
from schemas import EmailAnalysis


result = analyze_email_content(
    subject="Project Report Submission",
    sender="manager@example.com",
    body=(
        "Hi, please submit the final project report "
        "by October 20, 2026. This is important "
        "and requires your attention."
    )
)

assert isinstance(result, EmailAnalysis)

print(result.model_dump())