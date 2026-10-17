from pydantic import ValidationError
from schemas import EmailAnalysis

valid_email = EmailAnalysis(
    summary="Client requested a project update.",
    category="work",
    importance="high",
    deadlines=["2026-10-20"],
    action_required=True,
    confidence=0.95
)

print("Valid:", valid_email.model_dump())

try:
    EmailAnalysis(
        summary="Project update",
        category="shopping",
        importance="urgent",
        deadlines=[],
        action_required=True,
        confidence=1.5
    )
except ValidationError as error:
    print("Validation failed as expected:")
    print(error)