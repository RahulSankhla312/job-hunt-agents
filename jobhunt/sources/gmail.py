"""Gmail: send outreach, and read replies back.

OAuth only - no password, no app password, and the token lives on this machine.
Two scopes are requested and no more: ``gmail.send`` and ``gmail.readonly``.
Nothing here can delete or modify mail.

Reading is deliberately narrow. The Tracker needs to know which applications
got an answer; it has no business indexing a personal inbox, so every query is
scoped to threads this system started or to recognisable ATS senders.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from jobhunt.config import settings

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def token_path() -> Path:
    """Where the OAuth token lives. Follows JOBHUNT_DATA_DIR."""
    return settings().gmail_token_path


class GmailUnavailable(RuntimeError):
    """No OAuth client configured, or the token is missing or expired."""


@dataclass
class Message:
    id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime | None = None


def _client():
    """Build an authorised Gmail service, prompting for consent once."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise GmailUnavailable(f"google api libraries missing: {exc}") from exc

    secret = settings().google_client_secret_path
    token = token_path()
    creds = None

    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            log.warning("token refresh failed, re-authorising: %s", exc)
            creds = None

    if not creds or not creds.valid:
        if not secret.exists():
            raise GmailUnavailable(
                f"no OAuth client at {secret}. Create one in Google Cloud Console "
                "(Desktop app), download the JSON, and save it there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
        # Bounded, so a consent tab you closed does not hang the caller forever.
        creds = flow.run_local_server(port=0, timeout_seconds=300)
        if creds is None or not getattr(creds, "valid", False):
            raise GmailUnavailable("Google sign-in was not completed")
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def is_configured() -> tuple[bool, str]:
    secret = settings().google_client_secret_path
    if not secret.exists():
        return False, f"no OAuth client json at {secret}"
    token = token_path()
    if not token.exists():
        return False, "not yet connected - press Connect Gmail on the Connections page"
    return True, f"authorised ({token})"


def send(to: str, subject: str, body: str, *, reply_to: str | None = None) -> str:
    """Send a plain-text message. Returns the Gmail message id."""
    service = _client()
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    if reply_to:
        mime["Reply-To"] = reply_to

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("sent to %s (%s)", to, sent.get("id"))
    return sent.get("id", "")


_EMAIL_IN = re.compile(r"<([^>]+)>")


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def search(query: str, *, limit: int = 50) -> list[Message]:
    """Run a Gmail search and return message metadata only.

    Metadata format is requested explicitly: the Tracker classifies from the
    sender and subject, and there is no reason to pull message bodies out of a
    personal mailbox to do it.
    """
    service = _client()
    listing = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=limit)
        .execute()
    )

    out: list[Message] = []
    for stub in listing.get("messages", []):
        detail = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=stub["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        payload = detail.get("payload", {})
        sender = _header(payload, "From")
        received = None
        if ts := detail.get("internalDate"):
            try:
                received = datetime.fromtimestamp(int(ts) / 1000, tz=UTC)
            except (ValueError, OSError):
                received = None

        out.append(
            Message(
                id=detail.get("id", ""),
                thread_id=detail.get("threadId", ""),
                sender=(_EMAIL_IN.search(sender).group(1) if _EMAIL_IN.search(sender) else sender),
                subject=_header(payload, "Subject"),
                snippet=detail.get("snippet", "")[:300],
                received_at=received,
            )
        )
    return out


def job_related_query(days: int = 30) -> str:
    """A narrow query for application correspondence only.

    Scoped to the ATS and job-board senders this system actually applies
    through, plus obvious subject markers. Broad enough to catch replies,
    narrow enough that it is not a mailbox crawl.
    """
    after = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y/%m/%d")
    senders = (
        "greenhouse.io OR ashbyhq.com OR lever.co OR myworkdayjobs.com OR "
        "amazon.jobs OR smartrecruiters.com OR icims.com OR workable.com OR "
        "successfactors.com OR taleo.net"
    )
    subjects = (
        'subject:(application OR "thank you for applying" OR interview OR '
        '"your candidacy" OR "next steps" OR offer OR "we regret" OR '
        '"moving forward" OR recruiter)'
    )
    return f"after:{after} (from:({senders}) OR {subjects})"
