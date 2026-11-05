"""Meta WhatsApp Cloud API client.

Never log, return, or print WHATSAPP_ACCESS_TOKEN or raw credential material.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v25.0"
DEFAULT_LANGUAGE = "en_US"
PARAM_MAX_LEN = 1000
_TEMPLATE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TEMPLATE_CACHE_TTL_SEC = 300.0


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def whatsapp_enabled() -> bool:
    return _env_flag("WHATSAPP_ENABLED", "false")


def whatsapp_api_version() -> str:
    raw = (os.getenv("WHATSAPP_API_VERSION") or DEFAULT_API_VERSION).strip()
    return raw or DEFAULT_API_VERSION


def whatsapp_phone_number_id() -> str:
    return (os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()


def whatsapp_waba_id() -> str:
    return (os.getenv("WHATSAPP_WABA_ID") or "").strip()


def whatsapp_access_token() -> str:
    return (os.getenv("WHATSAPP_ACCESS_TOKEN") or "").strip()


def whatsapp_token_fingerprint() -> dict[str, Any]:
    """Safe token metadata only — never includes the raw token."""
    token = whatsapp_access_token()
    if not token:
        return {"present": False, "length": 0, "sha256_12": None}
    return {
        "present": True,
        "length": len(token),
        "sha256_12": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
    }


def whatsapp_test_recipient() -> str:
    return normalize_whatsapp_number(os.getenv("WHATSAPP_TEST_RECIPIENT") or "")


def whatsapp_template_language() -> str:
    return (os.getenv("WHATSAPP_TEMPLATE_LANGUAGE") or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE


def normalize_whatsapp_number(raw: str | None) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    return digits


def mask_recipient(raw: str | None) -> str:
    digits = normalize_whatsapp_number(raw)
    if len(digits) <= 4:
        return "****"
    return f"{'*' * max(0, len(digits) - 4)}{digits[-4:]}"


def truncate_param(value: str | None, max_len: int = PARAM_MAX_LEN) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_len:
        return text or "-"
    return text[: max_len - 1].rstrip() + "…"


def build_template_payload(
    *,
    to: str,
    template_name: str,
    language_code: str,
    body_params: list[str] | None = None,
) -> dict[str, Any]:
    recipient = normalize_whatsapp_number(to)
    if not recipient:
        raise ValueError("WhatsApp recipient is required")
    name = (template_name or "").strip()
    if not name:
        raise ValueError("WhatsApp template name is required")
    lang = (language_code or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE

    template: dict[str, Any] = {
        "name": name,
        "language": {"code": lang},
    }
    params = [truncate_param(p) for p in (body_params or [])]
    if params:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }
        ]

    return {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "template",
        "template": template,
    }


def _classify_meta_error(status_code: int | None, body: dict | None) -> tuple[str, str]:
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {}
    code = err.get("code")
    subcode = err.get("error_subcode")
    message = str(err.get("message") or "").strip()[:300]
    code_s = str(code) if code is not None else (str(status_code) if status_code else "unknown")

    text = message.lower()
    if status_code in {401, 403} or code in {190, 102}:
        return "auth_failed", message or "WhatsApp authentication failed"
    # Meta (#10) permission / asset access (e.g. Paid Message Account / WABA)
    if code == 10 or "cannot access" in text or "permission" in text:
        return "permission_denied", message or "WhatsApp permission denied for this asset"
    if status_code == 429 or code in {4, 80007, 130429}:
        return "rate_limited", message or "WhatsApp rate limited"
    if code in {132000, 132001, 132005, 132012, 132015, 132016} or "template" in text:
        return "pending_approval", message or "WhatsApp template unavailable or not approved"
    if status_code and status_code >= 500:
        return "transient_error", message or "WhatsApp server error"
    if subcode is not None:
        return f"{code_s}:{subcode}", message or "WhatsApp API error"
    return code_s, message or "WhatsApp API error"


def send_template(
    *,
    to: str | None = None,
    template_name: str,
    language_code: str | None = None,
    body_params: list[str] | None = None,
    http_client=None,
) -> dict[str, Any]:
    """Send an approved WhatsApp template. Never raises for Meta/network errors."""
    if not whatsapp_enabled():
        return {
            "ok": False,
            "status": "disabled",
            "meta_message_id": None,
            "error_code": "disabled",
            "error_message": "WhatsApp is disabled (WHATSAPP_ENABLED=false).",
            "recipient_masked": mask_recipient(to or whatsapp_test_recipient()),
            "template_name": template_name,
        }

    token = whatsapp_access_token()
    phone_id = whatsapp_phone_number_id()
    recipient = normalize_whatsapp_number(to) or whatsapp_test_recipient()
    lang = (language_code or whatsapp_template_language()).strip() or DEFAULT_LANGUAGE

    if not token or not phone_id:
        logger.warning("WhatsApp send skipped: missing access token or phone number id")
        return {
            "ok": False,
            "status": "misconfigured",
            "meta_message_id": None,
            "error_code": "misconfigured",
            "error_message": "WhatsApp credentials are not configured.",
            "recipient_masked": mask_recipient(recipient),
            "template_name": template_name,
        }
    if not recipient:
        return {
            "ok": False,
            "status": "invalid_recipient",
            "meta_message_id": None,
            "error_code": "invalid_recipient",
            "error_message": "WhatsApp recipient is missing.",
            "recipient_masked": "****",
            "template_name": template_name,
        }

    try:
        payload = build_template_payload(
            to=recipient,
            template_name=template_name,
            language_code=lang,
            body_params=body_params,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "status": "invalid_payload",
            "meta_message_id": None,
            "error_code": "invalid_payload",
            "error_message": str(exc)[:200],
            "recipient_masked": mask_recipient(recipient),
            "template_name": template_name,
        }

    version = whatsapp_api_version()
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        import httpx
    except ImportError:
        logger.error("httpx is not installed; cannot call WhatsApp Cloud API")
        return {
            "ok": False,
            "status": "misconfigured",
            "meta_message_id": None,
            "error_code": "missing_httpx",
            "error_message": "httpx is required for WhatsApp sends.",
            "recipient_masked": mask_recipient(recipient),
            "template_name": template_name,
        }

    client = http_client
    owns_client = client is None
    try:
        if owns_client:
            client = httpx.Client(timeout=20.0)
        assert client is not None
        response = client.post(url, headers=headers, json=payload)
        try:
            body = response.json()
        except Exception:
            body = None
        if response.status_code >= 400:
            err_code, err_msg = _classify_meta_error(response.status_code, body if isinstance(body, dict) else None)
            logger.warning(
                "WhatsApp send failed status=%s error_code=%s template=%s recipient=%s",
                response.status_code,
                err_code,
                template_name,
                mask_recipient(recipient),
            )
            status = "pending_approval" if err_code == "pending_approval" else "failed"
            if err_code == "auth_failed":
                status = "auth_failed"
            elif err_code == "rate_limited":
                status = "rate_limited"
            return {
                "ok": False,
                "status": status,
                "meta_message_id": None,
                "error_code": err_code,
                "error_message": err_msg,
                "recipient_masked": mask_recipient(recipient),
                "template_name": template_name,
            }

        message_id = None
        if isinstance(body, dict):
            messages = body.get("messages") or []
            if messages and isinstance(messages[0], dict):
                message_id = messages[0].get("id")
        logger.info(
            "WhatsApp template accepted template=%s recipient=%s meta_message_id=%s",
            template_name,
            mask_recipient(recipient),
            message_id or "none",
        )
        return {
            "ok": True,
            "status": "accepted",
            "meta_message_id": message_id,
            "error_code": None,
            "error_message": None,
            "recipient_masked": mask_recipient(recipient),
            "template_name": template_name,
        }
    except Exception as exc:
        # httpx.TimeoutException and network errors
        err_name = type(exc).__name__
        logger.warning(
            "WhatsApp send network error type=%s template=%s recipient=%s",
            err_name,
            template_name,
            mask_recipient(recipient),
        )
        return {
            "ok": False,
            "status": "timeout" if "Timeout" in err_name else "transient_error",
            "meta_message_id": None,
            "error_code": err_name,
            "error_message": "WhatsApp request failed due to a network or timeout error.",
            "recipient_masked": mask_recipient(recipient),
            "template_name": template_name,
        }
    finally:
        if owns_client and client is not None:
            client.close()


def get_template_status(template_name: str, *, http_client=None) -> dict[str, Any]:
    """Best-effort template status from WABA. Never exposes the access token."""
    name = (template_name or "").strip()
    if not name:
        return {"name": "", "status": "missing", "language": None}

    cached = _TEMPLATE_CACHE.get(name)
    now = time.time()
    if cached and now - cached[0] < _TEMPLATE_CACHE_TTL_SEC:
        return dict(cached[1])

    if not whatsapp_enabled():
        result = {"name": name, "status": "disabled", "language": None}
        return result

    token = whatsapp_access_token()
    waba = whatsapp_waba_id()
    if not token or not waba:
        return {"name": name, "status": "misconfigured", "language": None}

    try:
        import httpx
    except ImportError:
        return {"name": name, "status": "unknown", "language": None}

    version = whatsapp_api_version()
    url = f"https://graph.facebook.com/{version}/{waba}/message_templates"
    params = {"name": name, "limit": 10}
    headers = {"Authorization": f"Bearer {token}"}
    client = http_client
    owns_client = client is None
    try:
        if owns_client:
            client = httpx.Client(timeout=15.0)
        assert client is not None
        response = client.get(url, headers=headers, params=params)
        if response.status_code >= 400:
            logger.warning(
                "WhatsApp template status lookup failed http=%s template=%s",
                response.status_code,
                name,
            )
            result = {"name": name, "status": "unknown", "language": None}
            _TEMPLATE_CACHE[name] = (now, result)
            return dict(result)
        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            result = {"name": name, "status": "missing", "language": None}
            _TEMPLATE_CACHE[name] = (now, result)
            return dict(result)
        row = rows[0] if isinstance(rows[0], dict) else {}
        status = str(row.get("status") or "unknown").upper()
        language = None
        langs = row.get("language") or row.get("languages")
        if isinstance(langs, str):
            language = langs
        elif isinstance(row.get("language"), str):
            language = row.get("language")
        result = {
            "name": name,
            "status": status.lower(),
            "language": language,
            "approved": status == "APPROVED",
        }
        _TEMPLATE_CACHE[name] = (now, result)
        return dict(result)
    except Exception as exc:
        logger.warning(
            "WhatsApp template status lookup error type=%s template=%s",
            type(exc).__name__,
            name,
        )
        return {"name": name, "status": "unknown", "language": None}
    finally:
        if owns_client and client is not None:
            client.close()


def template_is_sendable(template_name: str, *, http_client=None) -> bool:
    """Return True when template looks approved, or status is unknown (try send)."""
    name = (template_name or "").strip()
    if not name:
        return False
    info = get_template_status(name, http_client=http_client)
    status = (info.get("status") or "").lower()
    if status in {"missing", "rejected", "disabled", "misconfigured", "paused"}:
        return False
    if status == "approved" or info.get("approved") is True:
        return True
    # unknown / pending: allow attempt; send_template maps Meta errors
    return status in {"unknown", "pending", "in_appeal", ""}


def _safe_meta_error(body: Any, status_code: int | None = None) -> dict[str, Any]:
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return {
            "http_status": status_code,
            "code": None,
            "error_subcode": None,
            "type": None,
            "message": None,
        }
    return {
        "http_status": status_code,
        "code": err.get("code"),
        "error_subcode": err.get("error_subcode"),
        "type": err.get("type"),
        "message": str(err.get("message") or "")[:300] or None,
    }


def _graph_get(
    client,
    *,
    url: str,
    token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
    )
    try:
        body = response.json()
    except Exception:
        body = None
    return {
        "http_status": response.status_code,
        "ok": response.status_code < 400,
        "body": body if isinstance(body, dict) else None,
        "error": None
        if response.status_code < 400
        else _safe_meta_error(body, response.status_code),
    }


def diagnose_whatsapp_config(*, http_client=None) -> dict[str, Any]:
    """
    Safe Meta connectivity/permission probe.

    Never includes WHATSAPP_ACCESS_TOKEN or Authorization values.
    Does not send a WhatsApp message.
    """
    version = whatsapp_api_version()
    phone_id = whatsapp_phone_number_id()
    waba_id = whatsapp_waba_id()
    template_name = (
        os.getenv("WHATSAPP_TEMPLATE_NAME") or "hello_world"
    ).strip() or "hello_world"
    language = (
        os.getenv("WHATSAPP_TEMPLATE_LANGUAGE") or DEFAULT_LANGUAGE
    ).strip() or DEFAULT_LANGUAGE
    recipient = whatsapp_test_recipient()
    token = whatsapp_access_token()
    fingerprint = whatsapp_token_fingerprint()

    expected_url = (
        f"https://graph.facebook.com/{version}/{phone_id}/messages"
        if phone_id
        else None
    )
    sample_payload = None
    if recipient:
        try:
            sample_payload = build_template_payload(
                to=recipient,
                template_name=template_name,
                language_code=language,
                body_params=None,
            )
        except ValueError as exc:
            sample_payload = {"error": str(exc)[:200]}

    report: dict[str, Any] = {
        "whatsapp_enabled": whatsapp_enabled(),
        "api_version": version,
        "phone_number_id": phone_id or None,
        "waba_id": waba_id or None,
        "template_name": template_name,
        "template_language": language,
        "recipient_masked": mask_recipient(recipient) if recipient else None,
        "token": fingerprint,
        "auth_scheme": "Bearer",
        "expected_messages_url": expected_url,
        "sample_template_payload": sample_payload,
        "graph_me": None,
        "graph_permissions": None,
        "debug_token": None,
        "graph_phone_number": None,
        "graph_waba": None,
        "diagnosis": [],
    }

    if not whatsapp_enabled():
        report["diagnosis"].append("WHATSAPP_ENABLED is false.")
        return report
    if not token:
        report["diagnosis"].append("WHATSAPP_ACCESS_TOKEN is missing.")
        return report
    if not phone_id:
        report["diagnosis"].append("WHATSAPP_PHONE_NUMBER_ID is missing.")

    try:
        import httpx
    except ImportError:
        report["diagnosis"].append("httpx is not installed.")
        return report

    client = http_client
    owns_client = client is None
    try:
        if owns_client:
            client = httpx.Client(timeout=20.0)
        assert client is not None

        me = _graph_get(
            client,
            url=f"https://graph.facebook.com/{version}/me",
            token=token,
            params={"fields": "id,name"},
        )
        me_body = me.get("body") if me.get("ok") else None
        report["graph_me"] = {
            "ok": me.get("ok"),
            "http_status": me.get("http_status"),
            "id": (me_body or {}).get("id") if isinstance(me_body, dict) else None,
            "name": (me_body or {}).get("name") if isinstance(me_body, dict) else None,
            "error": me.get("error"),
        }
        if me.get("ok") and isinstance(me_body, dict) and me_body.get("id"):
            report["token_subject"] = {
                "type_hint": "system_user_or_user",
                "id": me_body.get("id"),
                "name": me_body.get("name"),
            }
        elif me.get("error"):
            report["diagnosis"].append(
                "GET /me failed — token may be invalid or expired (Meta auth)."
            )

        debug = _graph_get(
            client,
            url=f"https://graph.facebook.com/{version}/debug_token",
            token=token,
            params={"input_token": token},
        )
        debug_body = debug.get("body") if debug.get("ok") else None
        debug_data = (
            debug_body.get("data")
            if isinstance(debug_body, dict) and isinstance(debug_body.get("data"), dict)
            else None
        )
        if isinstance(debug_data, dict):
            granular = debug_data.get("granular_scopes") or []
            target_ids: list[str] = []
            if isinstance(granular, list):
                for row in granular:
                    if not isinstance(row, dict):
                        continue
                    ids = row.get("target_ids") or []
                    if isinstance(ids, list):
                        target_ids.extend(str(x) for x in ids)
            report["debug_token"] = {
                "ok": True,
                "http_status": debug.get("http_status"),
                "app_id": debug_data.get("app_id"),
                "type": debug_data.get("type"),
                "is_valid": debug_data.get("is_valid"),
                "user_id": debug_data.get("user_id"),
                "scopes": debug_data.get("scopes") or [],
                "granular_scopes": granular,
                "granular_target_ids": sorted(set(target_ids)),
                "error": None,
            }
            if waba_id and waba_id not in target_ids and not target_ids:
                report["diagnosis"].append(
                    "debug_token shows WhatsApp scopes but no granular target_ids "
                    f"for WABA {waba_id}. In Meta Business Suite, assign this system user "
                    "to the WhatsApp Business Account (full control or message permissions) "
                    "and regenerate/use a token that includes that asset."
                )
            elif waba_id and target_ids and waba_id not in target_ids:
                report["diagnosis"].append(
                    f"Token granular target_ids={sorted(set(target_ids))} do not include "
                    f"configured WABA {waba_id}."
                )
        else:
            report["debug_token"] = {
                "ok": False,
                "http_status": debug.get("http_status"),
                "error": debug.get("error"),
            }

        perms = _graph_get(
            client,
            url=f"https://graph.facebook.com/{version}/me/permissions",
            token=token,
        )
        granted: list[str] = []
        declined: list[str] = []
        if perms.get("ok") and isinstance(perms.get("body"), dict):
            rows = perms["body"].get("data") or []
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    perm = str(row.get("permission") or "")
                    status = str(row.get("status") or "").lower()
                    if not perm:
                        continue
                    if status == "granted":
                        granted.append(perm)
                    else:
                        declined.append(perm)
            report["graph_permissions"] = {
                "ok": True,
                "http_status": perms.get("http_status"),
                "granted": sorted(granted),
                "declined_or_other": sorted(declined),
                "error": None,
            }
        else:
            report["graph_permissions"] = {
                "ok": False,
                "http_status": perms.get("http_status"),
                "granted": [],
                "declined_or_other": [],
                "error": perms.get("error"),
                "note": (
                    "me/permissions is often unavailable for system-user tokens; "
                    "missing scopes here does not by itself prove a send failure."
                ),
            }

        if phone_id:
            phone = _graph_get(
                client,
                url=f"https://graph.facebook.com/{version}/{phone_id}",
                token=token,
                params={
                    "fields": (
                        "id,display_phone_number,verified_name,quality_rating,account_mode"
                    ),
                },
            )
            phone_body = phone.get("body") if phone.get("ok") else None
            report["graph_phone_number"] = {
                "ok": phone.get("ok"),
                "http_status": phone.get("http_status"),
                "id": (phone_body or {}).get("id") if isinstance(phone_body, dict) else None,
                "display_phone_number": (
                    (phone_body or {}).get("display_phone_number")
                    if isinstance(phone_body, dict)
                    else None
                ),
                "verified_name": (
                    (phone_body or {}).get("verified_name")
                    if isinstance(phone_body, dict)
                    else None
                ),
                "account_mode": (
                    (phone_body or {}).get("account_mode")
                    if isinstance(phone_body, dict)
                    else None
                ),
                "error": phone.get("error"),
            }
            if not phone.get("ok"):
                err = phone.get("error") or {}
                report["diagnosis"].append(
                    "Token cannot read Phone Number ID "
                    f"{phone_id} (Meta code={err.get('code')}: {err.get('message')})."
                )

        if waba_id:
            waba = _graph_get(
                client,
                url=f"https://graph.facebook.com/{version}/{waba_id}",
                token=token,
                params={"fields": "id,name,currency,timezone_id,account_review_status"},
            )
            waba_body = waba.get("body") if waba.get("ok") else None
            report["graph_waba"] = {
                "ok": waba.get("ok"),
                "http_status": waba.get("http_status"),
                "id": (waba_body or {}).get("id") if isinstance(waba_body, dict) else None,
                "name": (waba_body or {}).get("name") if isinstance(waba_body, dict) else None,
                "account_review_status": (
                    (waba_body or {}).get("account_review_status")
                    if isinstance(waba_body, dict)
                    else None
                ),
                "error": waba.get("error"),
            }
            if not waba.get("ok"):
                err = waba.get("error") or {}
                code = err.get("code")
                msg = err.get("message") or ""
                report["diagnosis"].append(
                    f"Token cannot access WABA/Paid Message Account {waba_id} "
                    f"(Meta code={code}: {msg}). "
                    "Assign this token's system user (or app) to the WhatsApp Business Account "
                    "in Meta Business Suite, with whatsapp_business_messaging / "
                    "whatsapp_business_management."
                )
                if code == 10:
                    report["likely_root_cause"] = (
                        "Meta (#10) permission: the access token is valid enough to call Graph, "
                        "but its viewer cannot access WABA "
                        f"{waba_id}. This is an asset-sharing / system-user assignment issue, "
                        "not a Buddy payload or template-parameter bug."
                    )

        if version != "v25.0":
            report["diagnosis"].append(
                f"Configured API version is {version}; expected v25.0 for current Meta docs."
            )

        if not report["diagnosis"] and report.get("graph_phone_number", {}).get("ok"):
            report["diagnosis"].append(
                "Config probe looks healthy for phone/WABA read access. "
                "A successful POST /messages is still required to confirm sends."
            )
    except Exception as exc:
        logger.warning(
            "WhatsApp diagnose failed type=%s",
            type(exc).__name__,
        )
        report["diagnosis"].append(
            f"Diagnostic request failed ({type(exc).__name__})."
        )
    finally:
        if owns_client and client is not None:
            client.close()

    return report
