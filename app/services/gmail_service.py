"""Gmail API access: registering the push watch, resolving a push/reconcile
tick into actual new message ids via the History API, and fetching a
message's document attachments (PDF / Excel / CSV) plus its body text.
"""

import base64
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape as html_unescape
from html.parser import HTMLParser

import httpx

from app.config import settings
from app.services.google_auth import GOOGLE_API_RETRIES, gmail_client

logger = logging.getLogger(__name__)

USER_ID = "me"


@dataclass
class FetchedEmail:
    message_id: str  # Gmail's internal message id — unique within this mailbox
    sender: str
    subject: str
    received_at: datetime
    attachment_paths: list[str]  # saved PDF / Excel / CSV files
    body_text: str  # body as plain text (HTML tables kept as "a | b" rows), trimmed
    # Saved pictures in the email — inline signature logos, pasted
    # screenshots, attached photos / scans. Sent to the LLM with the email's
    # text, since a bank is often recognisable only by its logo. Named
    # "<message id>_img<n>_…" so the reader can find them again
    # (app/services/document_reader.py).
    image_paths: list[str] = field(default_factory=list)
    # Mailing-list / newsletter mail (List-Unsubscribe header) — never a bank
    # officer's email, so its pictures alone don't justify an LLM call.
    is_bulk: bool = False


# Documents an invoice can arrive as. .ics invites, signatures' vCards etc.
# are ignored.
DOCUMENT_EXTENSIONS = (".pdf", ".xlsx", ".xlsm", ".xls", ".csv", ".docx")
# Pictures the vision model can read (Claude: JPEG / PNG / GIF / WebP).
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
_MIN_IMAGE_BYTES = 800  # smaller = tracking pixels / spacers
_MAX_IMAGE_BYTES = 3_700_000  # the model's 5 MB limit, after base64
MAX_IMAGES = 8
_BODY_CHARS = 15000  # same cap as a spreadsheet's text (app/services/document_reader.py)
# Name the body is saved under when an email has no document attachment, so
# it goes through the graph like any other file (app/pipeline.py).
BODY_FILENAME = "email_body.txt"


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


class _HtmlToText(HTMLParser):
    """HTML body → plain text. A data table's cells are joined with " | "
    on one line per row — Gmail's own text/plain part puts every cell on its
    own line, which loses which quantity belongs to which product. A layout
    table (a cell holding another table, as Outlook wraps whole emails) keeps
    its cells as ordinary blocks of text."""

    _BLOCK = {"p", "div", "br", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"}
    _SKIP = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip = 0
        self._rows: list[dict] = []  # open <tr>s, innermost last
        self._cells: list[dict] = []  # open <td>/<th>s, innermost last

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "tr":
            self._rows.append({"cells": [], "layout": False, "depth": len(self._cells)})
        elif tag in ("td", "th") and self._rows:
            self._close_cell()  # an unclosed previous cell
            self._cells.append({"buf": [], "has_table": False})
        elif tag in self._BLOCK:
            if tag == "table" and self._cells:
                self._cells[-1]["has_table"] = True
            self._text("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr" and self._rows:
            self._close_cell()
            row = self._rows.pop()
            cells = [c for c in row["cells"] if c]
            if cells:
                self._text("\n" + ("\n" if row["layout"] else " | ").join(cells) + "\n")
        elif tag in self._BLOCK:
            self._text("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text(data)

    def _close_cell(self):
        """Ends the innermost open cell of the innermost open row, if any."""
        if not self._rows or len(self._cells) <= self._rows[-1]["depth"]:
            return
        cell = self._cells.pop()
        text = "".join(cell["buf"])
        if cell["has_table"]:
            self._rows[-1]["layout"] = True
            self._rows[-1]["cells"].append(text.strip())
        else:
            self._rows[-1]["cells"].append(" ".join(text.split()))

    def _text(self, data: str):
        (self._cells[-1]["buf"] if self._cells else self.out).append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.out).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _walk_parts(part: dict):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk_parts(sub)


def start_watch() -> tuple[str, int]:
    """Registers (or renews) the mailbox watch that makes Gmail publish a
    Pub/Sub notification on every new message. Returns
    (historyId, expiration_epoch_ms). A watch expires after Gmail's fixed
    cap (~7 days) — app/worker/scheduler.py renews it well before that so
    push notifications never silently stop.
    """
    if not settings.google_pubsub_topic:
        raise RuntimeError("GOOGLE_PUBSUB_TOPIC is not configured in .env")

    service = gmail_client()
    body = {
        "topicName": settings.google_pubsub_topic,
        "labelIds": [l.strip() for l in settings.gmail_watch_label_ids.split(",") if l.strip()],
    }
    resp = service.users().watch(userId=USER_ID, body=body).execute(num_retries=GOOGLE_API_RETRIES)
    return resp["historyId"], int(resp["expiration"])


def list_new_message_ids(start_history_id: str) -> tuple[list[str], str]:
    """Turns 'something changed since start_history_id' into the actual list
    of new message ids, via the Gmail History API. This is the step that a
    bare Pub/Sub notification cannot skip — the notification only carries a
    historyId, never the message itself.
    """
    service = gmail_client()
    message_ids: list[str] = []
    latest_history_id = start_history_id
    page_token = None

    while True:
        resp = (
            service.users()
            .history()
            .list(
                userId=USER_ID,
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
                pageToken=page_token,
            )
            .execute(num_retries=GOOGLE_API_RETRIES)
        )

        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                message_ids.append(added["message"]["id"])

        if "historyId" in resp:
            latest_history_id = resp["historyId"]

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    seen: set[str] = set()
    unique_ids = [m for m in message_ids if not (m in seen or seen.add(m))]
    return unique_ids, latest_history_id


def fetch_email(message_id: str) -> FetchedEmail:
    """Fetches one message, saves its document attachment(s) (PDF / Excel /
    CSV) to PDF_STORAGE_DIR, and returns them with the plain-text body.
    An email with no document attachment comes back with an empty
    `attachment_paths` — it's listed on the dashboard as skipped, without
    an LLM call."""
    service = gmail_client()
    message = (
        service.users()
        .messages()
        .get(userId=USER_ID, id=message_id, format="full")
        .execute(num_retries=GOOGLE_API_RETRIES)
    )

    payload = message.get("payload", {})
    headers = payload.get("headers", [])
    sender = _header(headers, "From")
    subject = _header(headers, "Subject")
    received_at = datetime.utcfromtimestamp(int(message["internalDate"]) / 1000)

    os.makedirs(settings.pdf_storage_dir, exist_ok=True)
    attachment_paths: list[str] = []
    plain_parts: list[str] = []
    html_parts: list[str] = []

    image_paths: list[str] = []

    for part in _walk_parts(payload):
        filename = part.get("filename") or ""
        body = part.get("body", {})
        mime = (part.get("mimeType") or "").lower()

        if mime in IMAGE_TYPES:
            # Inline (signature logo, pasted picture) or attached — both are
            # part of what the email shows.
            size = body.get("size") or 0
            if len(image_paths) < MAX_IMAGES and _MIN_IMAGE_BYTES <= size <= _MAX_IMAGE_BYTES:
                data = _part_data(service, message_id, body)
                if data:
                    base = os.path.splitext(filename)[0] or "inline"
                    safe_name = f"{message_id}_img{len(image_paths) + 1}_{base}{IMAGE_TYPES[mime]}"
                    path = os.path.join(settings.pdf_storage_dir, safe_name.replace("/", "_").replace("\\", "_"))
                    with open(path, "wb") as f:
                        f.write(_decode(data))
                    image_paths.append(path)
            continue

        if not filename:
            if body.get("data") and part.get("mimeType") in ("text/plain", "text/html"):
                text = _decode(body["data"]).decode("utf-8", errors="replace")
                (plain_parts if part["mimeType"] == "text/plain" else html_parts).append(text)
            continue
        if not filename.lower().endswith(DOCUMENT_EXTENSIONS):
            continue

        data = _part_data(service, message_id, body)
        if not data:
            continue

        safe_name = f"{message_id}_{filename}".replace("/", "_").replace("\\", "_")
        path = os.path.join(settings.pdf_storage_dir, safe_name)
        with open(path, "wb") as f:
            f.write(_decode(data))
        attachment_paths.append(path)

    return FetchedEmail(
        message_id=message_id,
        sender=sender,
        subject=subject,
        received_at=received_at,
        attachment_paths=attachment_paths,
        body_text=_body_text(plain_parts, html_parts)[:_BODY_CHARS],
        image_paths=image_paths + _download_linked_images(message_id, "\n".join(html_parts), len(image_paths)),
        is_bulk=bool(_header(headers, "List-Unsubscribe")),
    )


_IMG_SRC_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["'](https?://[^"'\s>]+)""", re.IGNORECASE)


def _download_linked_images(message_id: str, html: str, already: int) -> list[str]:
    """Pictures the email's HTML loads from the web instead of embedding —
    bank signatures often link their logo (Gmail keeps them as links, or as
    googleusercontent.com proxy links, when forwarding). Downloaded so the
    logo can be checked like an embedded one; tracking pixels, non-images
    and anything unreachable are skipped."""
    paths: list[str] = []
    urls = list(dict.fromkeys(_IMG_SRC_RE.findall(html)))  # unique, in order
    for url in urls:
        if already + len(paths) >= MAX_IMAGES:
            break
        url = html_unescape(url)
        try:
            with httpx.stream("GET", url, timeout=10, follow_redirects=True) as resp:
                mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if resp.status_code != 200 or mime not in IMAGE_TYPES:
                    continue
                data = b""
                for chunk in resp.iter_bytes():
                    data += chunk
                    if len(data) > _MAX_IMAGE_BYTES:
                        break
        except httpx.HTTPError as exc:
            logger.info("Linked image %s not downloaded: %s", url[:120], exc)
            continue
        if not _MIN_IMAGE_BYTES <= len(data) <= _MAX_IMAGE_BYTES:
            continue
        path = os.path.join(
            settings.pdf_storage_dir, f"{message_id}_img{already + len(paths) + 1}_linked{IMAGE_TYPES[mime]}"
        )
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return paths


def _part_data(service, message_id: str, body: dict) -> str:
    """A part's base64url content: fetched separately for a real attachment,
    or inlined in the message for a small one."""
    attachment_id = body.get("attachmentId")
    if not attachment_id:
        return body.get("data", "")
    attachment = (
        service.users()
        .messages()
        .attachments()
        .get(userId=USER_ID, messageId=message_id, id=attachment_id)
        .execute(num_retries=GOOGLE_API_RETRIES)
    )
    return attachment.get("data", "")


def _body_text(plain_parts: list[str], html_parts: list[str]) -> str:
    """The HTML version when it has a table (kept row by row) or there's no
    plain-text version; otherwise the plain text."""
    html = "\n".join(html_parts)
    if html and ("<table" in html.lower() or not plain_parts):
        return html_to_text(html)
    return "\n".join(plain_parts).strip()


_ADDRESS_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
# A forwarded message's "From:" line (Gmail "From: Name <a@b.com>", Outlook
# "From: Name/Branch <a@b.com>"); the address can wrap onto the next line.
_FORWARD_FROM_RE = re.compile(r"^\s*\*?From:\*?\s*(.{0,200}?@[\w.-]+)", re.IGNORECASE | re.MULTILINE | re.DOTALL)


def chain_senders(email: FetchedEmail) -> list[str]:
    """Every sender address in the email's forward chain, lower-cased, the
    outermost (who sent it to us) first and the original author last."""
    found = _ADDRESS_RE.findall(email.sender)
    for m in _FORWARD_FROM_RE.finditer(email.body_text):
        found += _ADDRESS_RE.findall(m.group(1))[:1]
    return list(dict.fromkeys(a.lower().strip(".") for a in found))


def save_body_document(email: FetchedEmail) -> str:
    """Saves the email itself (its headers + body) as a text file next to the
    attachments, for an email that carries its content in the body instead
    of an attachment — e.g. a bank officer's delivery instruction forwarded
    on to us. Returns the path."""
    path = os.path.join(settings.pdf_storage_dir, f"{email.message_id}_{BODY_FILENAME}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"From: {email.sender}\n"
            f"Subject: {email.subject}\n"
            f"Date: {email.received_at:%Y-%m-%d %H:%M} UTC\n\n"
            f"{email.body_text}\n"
        )
    return path
