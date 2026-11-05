"""CLI wrapper for safe WhatsApp diagnostics. Never prints access tokens."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env", override=False)


def main() -> int:
    from whatsapp_service import diagnose_whatsapp_config

    report = diagnose_whatsapp_config()
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
