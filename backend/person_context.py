"""Ask About a Person — grounded context from stored mail.

Feature flag: ASK_ABOUT_PERSON_ENABLED (default false).

Resolves a person from From headers for the authenticated mailbox only.
Does not sync Gmail, send mail, or merge contacts unless a verified alias
row already exists. Outgoing messages are included only when they already
share a thread with a matching From.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_SENDER_ROWS = 40
MAX_THREADS = 8
MAX_MESSAGES = 40
CANDIDATE_PAGE = 5
CONSUMER_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
}
PRONOUN_FOLLOWUP_RE = re.compile(
    r"\b(he|she|him|her|his|hers|they|them|their|that person|this person)\b",
    re.I,
)
_NAME = r"([A-Za-z][\w.'-]{1,}(?:\s+[A-Za-z][\w.'-]{1,})?)"
PERSON_INTENT_RE = re.compile(
    r"\b(?:"
    rf"what did\s+{_NAME}\s+(?:last\s+)?(?:e-?mail|mail|message|write|send|say)"
    rf"|conversations?\s+with\s+{_NAME}"
    rf"|(?:e-?mails?|messages?|threads?)\s+(?:with|from)\s+{_NAME}"
    rf"|pending\s+(?:tasks?|actions?|items?)?\s*with\s+{_NAME}"
    rf"|commitments?\s+did\s+{_NAME}\s+make"
    rf"|deadlines?\s+did\s+{_NAME}\s+mention"
    rf"|(?:waiting|wait)\s+for\s+(?:a\s+)?(?:reply|response)\s+from\s+{_NAME}"
    rf"|are we waiting\b.{{0,40}}\bfrom\s+{_NAME}"
    rf"|follow(?:\s*|-)?up\s+(?:on\s+)?with\s+{_NAME}"
    rf"|about\s+{_NAME}(?:'s)?\s+(?:e-?mails?|messages?|conversations?)"
    r")\b",
    re.I,
)
EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"
)
CLAIM_RE = re.compile(
    r"\b(commit(?:ted|ment|s)?|promis(?:e|ed|es)|guarantee[ds]?|agreed to)\b",
    re.I,
)
_NAME_STOP = {
    "me",
    "my",
    "our",
    "the",
    "a",
    "an",
    "this",
    "that",
    "them",
    "they",
    "him",
    "her",
    "he",
    "she",
    "his",
    "hers",
    "someone",
    "somebody",
    "you",
    "i",
    "person",
}


def ask_about_person_enabled() -> bool:
    return os.getenv("ASK_ABOUT_PERSON_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_person_question(question: str) -> bool:
    """True only for person-history questions, not inbox-wide or general ones."""
    return bool(PERSON_INTENT_RE.search(question or ""))


def is_pronoun_person_followup(question: str) -> bool:
    """Pronoun follow-up with no new name or email in the question."""
    if extract_person_term(question):
        return False
    return bool(PRONOUN_FOLLOWUP_RE.search(question or ""))


def extract_person_term(question: str) -> str | None:
    text = question or ""
    emails = list(dict.fromkeys(match.group(1) for match in EMAIL_RE.finditer(text)))
    if len(emails) == 1:
        return emails[0]
    if len(emails) > 1:
        return None
    match = PERSON_INTENT_RE.search(text)
    if not match:
        return None
    raw = next((group for group in match.groups() if group), "")
    cleaned = " ".join(raw.split()).strip(" .,;:?")
    parts = cleaned.split()
    trail = {
        "last",
        "email",
        "emails",
        "mail",
        "mails",
        "message",
        "messages",
        "recent",
        "about",
        "me",
    }
    while len(parts) > 1 and parts[-1].lower() in trail:
        parts.pop()
    cleaned = " ".join(parts)
    if not cleaned or cleaned.lower() in _NAME_STOP:
        return None
    if cleaned.lower().split()[0] in _NAME_STOP:
        return None
    return cleaned


def parse_sender_identity(sender: str | None) -> tuple[str, str]:
    text = (sender or "").strip()
    email_match = EMAIL_RE.search(text)
    email = email_match.group(1).lower() if email_match else ""
    if "<" in text:
        name = text.split("<", 1)[0].strip().strip('"')
    elif email and text.lower() == email:
        name = email.split("@", 1)[0]
    else:
        name = text
    return (name or email or "Unknown"), email


def _search_terms(term: str, aliases: list[dict]) -> list[str]:
    """Exact-address questions also search verified aliases of that address."""
    terms = [term]
    if "@" not in (term or ""):
        return terms
    needle = term.strip().lower()
    known = {needle}
    for row in aliases:
        if row.get("verified") is not True:
            continue
        alias = str(row.get("alias_email") or "").strip().lower()
        canonical = str(row.get("canonical_email") or "").strip().lower()
        if needle not in {alias, canonical}:
            continue
        for extra in (alias, canonical):
            if extra and extra not in known:
                known.add(extra)
                terms.append(extra)
    return terms


def _canonical_email(email: str, aliases: list[dict]) -> str:
    target = (email or "").lower()
    for row in aliases:
        if row.get("verified") is not True:
            continue
        alias = str(row.get("alias_email") or "").strip().lower()
        canonical = str(row.get("canonical_email") or "").strip().lower()
        if alias and canonical and alias == target:
            return canonical
    return target


def _domain(email: str) -> str:
    if "@" not in (email or ""):
        return ""
    return email.rsplit("@", 1)[-1].strip().lower()


def _company_label(email: str) -> str | None:
    domain = _domain(email)
    if not domain or domain in CONSUMER_DOMAINS:
        return None
    return domain


def _name_tokens(name: str) -> list[str]:
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z][\w.'-]*", name or "")
    ]


def _match_reason(person: dict, term: str, aliases: list[dict]) -> str | None:
    """Why this address matches. None means it is not a plausible identity."""
    needle = (term or "").strip().lower()
    if not needle:
        return None
    addresses = {addr.lower() for addr in person.get("addresses") or []}
    canonical = str(person.get("email") or "").strip().lower()
    if "@" in needle:
        if needle == canonical:
            return "Exact email"
        if needle in addresses:
            for row in aliases:
                if row.get("verified") is not True:
                    continue
                alias = str(row.get("alias_email") or "").strip().lower()
                canon = str(row.get("canonical_email") or "").strip().lower()
                if alias == needle and canon == canonical:
                    return "Verified alias"
            return "Exact email"
        return None

    if " " not in needle and "." in needle:
        domains = {_domain(addr) for addr in addresses}
        if needle in domains and needle not in CONSUMER_DOMAINS:
            return "Same company domain"
        return None

    tokens = _name_tokens(str(person.get("name") or ""))
    parts = [part for part in needle.split() if part]
    if parts and parts == tokens:
        return "Exact name"
    if len(parts) == 1 and parts[0] in tokens:
        if tokens and tokens[0] == parts[0]:
            return "Same first name"
        if len(tokens) > 1 and tokens[-1] == parts[0]:
            return "Same last name"
        return "Name match"
    if len(parts) > 1 and all(part in tokens for part in parts):
        return "Name match"

    if len(parts) == 1:
        for addr in addresses:
            domain = _domain(addr)
            if domain in CONSUMER_DOMAINS:
                continue
            label = domain.split(".", 1)[0]
            if label and label == parts[0]:
                return "Same company domain"
    return None


def group_people(
    rows: list[dict],
    *,
    term: str,
    aliases: list[dict] | None = None,
    account_email: str = "",
) -> list[dict]:
    """Group stored From headers. Unverified aliases stay separate."""
    alias_rows = list(aliases or [])
    account = (account_email or "").strip().lower()
    grouped: dict[str, dict] = {}
    for row in rows:
        if account:
            owner = str(row.get("gmail_account_email") or "").strip().lower()
            if owner and owner != account:
                continue
        name, email = parse_sender_identity(row.get("sender"))
        if not email or (account and email == account):
            continue
        key = _canonical_email(email, alias_rows)
        person = grouped.get(key)
        if person is None:
            person = {
                "id": key,
                "name": name,
                "email": key,
                "addresses": [],
            }
            grouped[key] = person
        if email not in person["addresses"]:
            person["addresses"].append(email)
        if name and "@" not in name and len(name) > len(person["name"]):
            person["name"] = name
        for row_alias in alias_rows:
            if row_alias.get("verified") is not True:
                continue
            if str(row_alias.get("canonical_email") or "").lower() != key:
                continue
            alias = str(row_alias.get("alias_email") or "").strip().lower()
            if alias and alias not in person["addresses"]:
                person["addresses"].append(alias)

    people = []
    for person in grouped.values():
        reason = _match_reason(person, term, alias_rows)
        if not reason:
            continue
        person["match_reason"] = reason
        people.append(person)
    people.sort(key=lambda item: (item["name"].lower(), item["email"]))
    return people


def _analysis(email: dict) -> dict:
    analyses = email.get("email_analyses") or []
    if isinstance(analyses, list):
        return analyses[0] if analyses else {}
    return analyses if isinstance(analyses, dict) else {}


def _ms(email: dict) -> int:
    try:
        return int(email.get("internal_date") or 0)
    except (TypeError, ValueError):
        return 0


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    kept: list[dict] = []
    for row in rows:
        key = str(row.get("gmail_message_id") or row.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def _load_aliases(account: str) -> tuple[list[dict], bool]:
    try:
        from db import get_supabase_client

        client = get_supabase_client()
        response = (
            client.table("person_contact_aliases")
            .select("canonical_email, alias_email, verified")
            .eq("gmail_account_email", account)
            .execute()
        )
        return list(response.data or []), True
    except Exception as exc:
        logger.info("Person alias table unavailable: %s", str(exc)[:160])
        return [], False


def _match_followup(
    states: list[dict],
    *,
    thread_ids: set[str],
    email_ids: set[str],
) -> dict | None:
    for row in states:
        if str(row.get("source_type") or "") != "followup":
            continue
        if str(row.get("status") or "") in {"dismissed", "completed"}:
            continue
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        thread_id = str(meta.get("gmail_thread_id") or "")
        source_id = str(meta.get("source_email_id") or row.get("source_id") or "")
        if thread_id and thread_id in thread_ids:
            return row
        if source_id and source_id in email_ids:
            return row
    return None


def _followup_label(row: dict | None) -> str:
    if not row:
        return "No open follow-up."
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    state = str(meta.get("followup_state") or "")
    if state == "awaiting_response":
        return "Waiting on their reply."
    if state == "pending_reply":
        return "Waiting on your reply."
    status = str(row.get("status") or "pending")
    return f"Follow-up status: {status}."


def _evidence_block(messages: list[dict], account: str) -> str:
    from briefing_followup import message_direction

    lines: list[str] = []
    ordered = sorted(messages, key=_ms)
    for email in ordered:
        analysis = _analysis(email)
        direction = message_direction(email, account)
        deadlines = analysis.get("deadlines") or []
        lines.append(
            "\n".join(
                [
                    f"id: {email.get('id') or ''}",
                    f"direction: {direction}",
                    f"date: {email.get('received_at') or ''}",
                    f"subject: {email.get('subject') or ''}",
                    f"sender: {email.get('sender') or ''}",
                    f"summary: {analysis.get('summary') or ''}",
                    f"deadlines: {deadlines}",
                    f"action_required: {analysis.get('action_required')}",
                    f"snippet: {str(email.get('snippet') or '')[:240]}",
                ]
            )
        )
    return "\n---\n".join(lines)


def _deterministic_answer(
    *,
    person: dict,
    messages: list[dict],
    pending: list[str],
    deadlines: list[str],
    sent_incomplete: bool,
    alias_note: str | None,
) -> str:
    if not messages:
        text = (
            f"No stored conversation with {person.get('name') or 'this person'} "
            f"({person.get('email') or 'unknown address'}). "
            "Outgoing mail can be missing when SENT history was not synced."
        )
    else:
        newest = max(messages, key=_ms)
        text = (
            f"{person.get('name')} ({person.get('email')}). "
            f"Last stored interaction: {newest.get('subject') or 'Untitled'} "
            f"on {newest.get('received_at') or 'an unknown date'}."
        )
        if pending:
            text += " Pending: " + "; ".join(pending) + "."
        else:
            text += " No pending action was marked on these stored emails."
        if deadlines:
            text += " Deadlines in stored analyses: " + "; ".join(deadlines) + "."
        else:
            text += " No deadline was found in the stored analyses."
        text += " No commitment was found beyond the stored summaries and snippets."
        if sent_incomplete:
            text += (
                " Outgoing messages may be incomplete because SENT synchronization "
                "only stores mail already on a synced thread."
            )
    if alias_note:
        text += " " + alias_note
    return text


def _unsupported_claim(answer: str, evidence: str) -> bool:
    for match in CLAIM_RE.finditer(answer or ""):
        window_start = max(0, match.start() - 48)
        window = answer[window_start : match.end() + 48]
        if re.search(r"\b(no|not|none|wasn't|weren't|without)\b", window, re.I):
            continue
        if not CLAIM_RE.search(evidence or ""):
            return True
    return False


def synthesize_person_answer(question: str, evidence: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise RuntimeError("missing_openai_key")
    llm = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
        api_key=os.getenv("OPENAI_API_KEY"),
    )
    messages = [
        SystemMessage(
            content=(
                "You answer questions about one person using only the evidence block. "
                "Every factual claim must appear in the evidence. "
                "Call a claim confirmed when one message states it, and inferred when "
                "you are only combining stored lines. "
                "If a commitment, promise, or deadline is not in the evidence, say it "
                "was not found. Do not invent dates, amounts, or promises. "
                "Cite source ids in brackets."
            )
        ),
        HumanMessage(
            content=f"Evidence:\n{evidence}\n\nQuestion: {question}"
        ),
    ]
    response = llm.invoke(messages)
    return str(response.content or "").strip()


def _rows_for_person(rows: list[dict], person: dict, aliases: list[dict]) -> list[dict]:
    addresses = {addr.lower() for addr in person.get("addresses") or []}
    matched = []
    for row in rows:
        _name, email = parse_sender_identity(row.get("sender"))
        if email in addresses or _canonical_email(email, aliases) == person.get("email"):
            matched.append(row)
    return matched


def _candidate_card(person: dict, rows: list[dict], aliases: list[dict]) -> dict:
    own = _rows_for_person(rows, person, aliases)
    newest = max(own, key=_ms) if own else None
    analysis = _analysis(newest) if newest else {}
    summary = str(analysis.get("summary") or "").strip()
    subject = str((newest or {}).get("subject") or "").strip()
    topic = subject or summary
    if len(topic) > 140:
        topic = topic[:137].rstrip() + "..."
    context = summary[:177].rstrip() + "..." if len(summary) > 180 else summary
    return {
        "id": person["email"],
        "name": person["name"],
        "email": person["email"],
        "company": _company_label(str(person.get("email") or "")),
        "last_interaction": str((newest or {}).get("received_at") or ""),
        "last_topic": topic,
        "match_reason": person.get("match_reason") or "Name match",
        "context": context or None,
    }


def _candidate_page(
    people: list[dict],
    rows: list[dict],
    aliases: list[dict],
    *,
    term: str,
    offset: int,
    alias_note: str | None,
    domain_only: bool,
) -> dict[str, Any]:
    total = len(people)
    start = max(0, int(offset or 0))
    page = people[start : start + CANDIDATE_PAGE]
    end = start + len(page)
    if domain_only and total == 1:
        answer = (
            f"A domain does not uniquely identify a person. "
            f"Confirm this contact from synchronized Gmail history."
        )
    else:
        answer = (
            f"I found {total} people matching “{term}” in synchronized Gmail history. "
            f"Showing {start + 1}–{end}. Which one should I use?"
        )
    if alias_note:
        answer = f"{answer} {alias_note}"
    return {
        "mode": "candidates",
        "answer": answer,
        "candidates": [_candidate_card(person, rows, aliases) for person in page],
        "candidate_total": total,
        "candidate_offset": start,
        "candidate_has_more": end < total,
        "person": None,
        "sources": [],
        "alias_note": alias_note,
    }


def retrieve_person_context(
    question: str,
    *,
    gmail_account_email: str,
    search_emails: Callable[..., list] | None = None,
    list_threads: Callable[..., list] | None = None,
    list_states: Callable[..., list] | None = None,
    list_aliases: Callable[..., Any] | None = None,
    today=None,
    limit: int = MAX_MESSAGES,
    offset: int = 0,
    selected_contact_id: str | None = None,
    bound_contact_id: str | None = None,
    candidate_offset: int = 0,
) -> dict[str, Any]:
    account = (gmail_account_email or "").strip()
    if not account:
        raise PermissionError("missing_account")

    forced_id = (selected_contact_id or "").strip().lower()
    named_term = extract_person_term(question)
    if forced_id:
        if "@" not in forced_id or not EMAIL_RE.fullmatch(forced_id):
            return {
                "mode": "denied",
                "answer": "That contact identifier is not a valid email address.",
                "candidates": [],
                "person": None,
                "sources": [],
            }
        term = forced_id
    elif named_term:
        term = named_term
    elif is_pronoun_person_followup(question) and (bound_contact_id or "").strip():
        forced_id = bound_contact_id.strip().lower()
        if "@" not in forced_id or not EMAIL_RE.fullmatch(forced_id):
            return {
                "mode": "denied",
                "answer": "That contact identifier is not a valid email address.",
                "candidates": [],
                "person": None,
                "sources": [],
            }
        term = forced_id
    elif is_pronoun_person_followup(question):
        return {
            "mode": "needs_name",
            "answer": (
                "Which person should I use? Give a name or email address. "
                "I only search this account's synchronized Gmail history."
            ),
            "candidates": [],
            "person": None,
            "sources": [],
        }
    else:
        return {
            "mode": "needs_name",
            "answer": "Which person should I use? Give a name or email address.",
            "candidates": [],
            "person": None,
            "sources": [],
        }

    if list_aliases is None:
        aliases, alias_available = _load_aliases(account)
    else:
        loaded = list_aliases(account)
        if isinstance(loaded, tuple):
            aliases, alias_available = list(loaded[0] or []), bool(loaded[1])
        else:
            aliases, alias_available = list(loaded or []), True

    if search_emails is None:
        from db import search_emails_for_chat as search_emails

    from briefing_service import date_iso_to_internal_ms_range
    from chat_service import parse_date_windows

    windows = parse_date_windows(question, today=today)
    date_from = windows[0].date_from.isoformat() if len(windows) == 1 else None
    date_to = windows[0].date_to.isoformat() if len(windows) == 1 else None
    start_ms, end_ms = date_iso_to_internal_ms_range(date_from, date_to)
    rows: list[dict] = []
    seen_ids: set[str] = set()
    for search_term in _search_terms(term, aliases):
        found = search_emails(
            gmail_account_email=account,
            sender_contains=search_term,
            internal_date_from_ms=start_ms,
            internal_date_to_ms=end_ms,
            limit=MAX_SENDER_ROWS,
            in_inbox_only=False,
        )
        for row in found or []:
            key = str(row.get("id") or row.get("gmail_message_id") or id(row))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            rows.append(row)
            if len(rows) >= MAX_SENDER_ROWS:
                break
        if len(rows) >= MAX_SENDER_ROWS:
            break
    people = group_people(rows, term=term, aliases=aliases, account_email=account)
    alias_note = None
    if not alias_available and len(people) > 1:
        alias_note = (
            "These addresses were kept separate. "
            "Alias linking is unavailable until the contact alias table exists."
        )

    if len(people) == 0:
        if forced_id:
            return {
                "mode": "denied",
                "answer": (
                    "That contact is not in this account's synchronized Gmail history."
                ),
                "candidates": [],
                "person": None,
                "sources": [],
            }
        return {
            "mode": "empty",
            "answer": (
                f"No stored mail from {term} in this account's synchronized Gmail history. "
                "Only synchronized Gmail history for this account was searched. "
                "Outgoing-only threads can be missing because SENT sync is partial."
            ),
            "candidates": [],
            "person": {
                "name": term,
                "email": term if "@" in term else "",
                "addresses": [term] if "@" in term else [],
                "summary": "No stored conversation.",
                "last_interaction": "",
                "pending_actions": [],
                "follow_up": "No open follow-up.",
                "emails": [],
                "sent_history_incomplete": True,
                "draft_email_id": None,
                "recommendation_key": None,
                "alias_note": alias_note,
            },
            "sources": [],
            "evidence": "",
            "messages": [],
        }

    domain_only = all(
        person.get("match_reason") == "Same company domain" for person in people
    )
    if len(people) > 1 or (domain_only and not forced_id):
        return _candidate_page(
            people,
            rows,
            aliases,
            term=term,
            offset=candidate_offset,
            alias_note=alias_note,
            domain_only=domain_only,
        )

    person = people[0]
    matched = _rows_for_person(rows, person, aliases)

    thread_ids: list[str] = []
    for row in matched:
        thread_id = str(row.get("gmail_thread_id") or "")
        if thread_id and thread_id not in thread_ids:
            thread_ids.append(thread_id)
        if len(thread_ids) >= MAX_THREADS:
            break

    if list_threads is None:
        from db import list_thread_messages as list_threads

    messages = list(matched)
    for thread_id in thread_ids:
        try:
            messages.extend(
                list_threads(gmail_account_email=account, thread_id=thread_id) or []
            )
        except Exception:
            logger.exception("Person thread load failed")
    messages = _dedupe(messages)
    if start_ms is not None or end_ms is not None:
        filtered = []
        for row in messages:
            stamp = _ms(row)
            if start_ms is not None and stamp < start_ms:
                continue
            if end_ms is not None and stamp >= end_ms:
                continue
            filtered.append(row)
        messages = filtered
    messages.sort(key=_ms, reverse=True)
    total = len(messages)
    start = max(0, int(offset))
    messages = messages[start : start + max(1, int(limit))]

    from briefing_followup import message_direction

    outgoing = [
        row
        for row in messages
        if message_direction(row, account) == "outgoing"
    ]
    incoming = [
        row
        for row in messages
        if message_direction(row, account) == "incoming"
    ]
    sent_incomplete = len(outgoing) == 0
    own_ids = {str(row.get("id") or "") for row in matched}
    pending: list[str] = []
    deadlines: list[str] = []
    for row in messages:
        if str(row.get("id") or "") not in own_ids:
            continue
        analysis = _analysis(row)
        if analysis.get("action_required") is True:
            label = str(row.get("subject") or "Email")
            summary = str(analysis.get("summary") or "").strip()
            pending.append((f"{label}: {summary}" if summary else label)[:180])
        for deadline in analysis.get("deadlines") or []:
            text = str(deadline).strip()
            if text and text not in deadlines:
                deadlines.append(text)

    if list_states is None:
        try:
            from db import list_recommendation_states_for_account as list_states

            states = list_states(gmail_account_email=account) or []
        except Exception:
            logger.info("Person follow-up state lookup skipped")
            states = []
    else:
        states = list_states(gmail_account_email=account) or []

    email_ids = {str(row.get("id") or "") for row in messages}
    follow = _match_followup(
        states,
        thread_ids=set(thread_ids),
        email_ids=email_ids,
    )
    newest = max(messages, key=_ms) if messages else None
    last_interaction = ""
    if newest:
        last_interaction = (
            f"{newest.get('received_at') or 'Unknown date'} · "
            f"{newest.get('subject') or 'Untitled'}"
        )
    draft_email_id = str(incoming[0].get("id") or "") if incoming else None
    sources = [
        {
            "email_id": str(row.get("id") or ""),
            "subject": row.get("subject"),
            "sender": row.get("sender"),
            "gmail_thread_id": row.get("gmail_thread_id"),
        }
        for row in messages
        if row.get("id")
    ][:8]
    evidence = _evidence_block(messages, account)
    summary = _deterministic_answer(
        person=person,
        messages=messages,
        pending=pending[:5],
        deadlines=deadlines[:5],
        sent_incomplete=sent_incomplete,
        alias_note=None,
    )
    card = {
        "name": person["name"],
        "email": person["email"],
        "addresses": person["addresses"],
        "summary": summary,
        "last_interaction": last_interaction,
        "pending_actions": pending[:5],
        "follow_up": _followup_label(follow),
        "emails": sources,
        "sent_history_incomplete": sent_incomplete,
        "draft_email_id": draft_email_id,
        "recommendation_key": (
            str(follow.get("recommendation_key") or "") if follow else None
        )
        or None,
        "alias_note": alias_note,
        "truncated": total > len(messages) + start,
        "matched_messages": total,
    }
    return {
        "mode": "ready" if messages else "empty",
        "answer": summary,
        "candidates": [],
        "person": card,
        "sources": sources,
        "evidence": evidence,
        "messages": messages,
        "alias_note": alias_note,
    }


def render_person_answer(
    question: str,
    result: dict | None,
    history: list | None = None,
) -> dict[str, Any]:
    """Chat payload. Model text is dropped when it adds an unsupported commitment."""
    del history
    payload = result or {}
    mode = payload.get("mode")
    if mode in {"needs_name", "candidates", "denied"}:
        return {
            "answer": payload.get("answer") or "Which person should I use?",
            "sources": [],
            "draft": None,
            "candidates": list(payload.get("candidates") or []),
            "candidate_total": payload.get("candidate_total") or 0,
            "candidate_offset": payload.get("candidate_offset") or 0,
            "candidate_has_more": bool(payload.get("candidate_has_more")),
            "person": None,
        }
    person = payload.get("person")
    evidence = str(payload.get("evidence") or "")
    fallback = str(payload.get("answer") or "")
    if mode == "empty" or not evidence.strip():
        return {
            "answer": fallback,
            "sources": list(payload.get("sources") or []),
            "draft": None,
            "candidates": [],
            "person": person,
        }
    answer = fallback
    try:
        drafted = synthesize_person_answer(question, evidence)
        if drafted and not _unsupported_claim(drafted, evidence):
            answer = drafted
            if person and person.get("sent_history_incomplete"):
                answer += (
                    " Outgoing mail may be incomplete because SENT history is partial."
                )
        elif drafted and _unsupported_claim(drafted, evidence):
            answer = fallback
    except Exception:
        logger.info("Person synthesis fell back to stored evidence")
        answer = fallback
    if person is not None:
        person = dict(person)
        person["summary"] = answer
    return {
        "answer": answer,
        "sources": list(payload.get("sources") or []),
        "draft": None,
        "candidates": [],
        "person": person,
    }
