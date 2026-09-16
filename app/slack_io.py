"""Everything that talks to Slack: reading the thread, downloading attachments,
building Claude content blocks from what we found, and posting replies.
"""
from __future__ import annotations

import base64
import io
import logging
import re

import requests
from slack_sdk import WebClient

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
GOLD_RE = re.compile(r"\bgold\b", re.IGNORECASE)
URL_RE = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>")

TEXT_TYPES = {"text/plain", "text/markdown", "text/csv"}
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_FILE_BYTES = 20 * 1024 * 1024


def is_gold_request(text: str) -> bool:
    return bool(GOLD_RE.search(MENTION_RE.sub("", text or "")))


def _download(url: str, token: str) -> bytes:
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    if len(resp.content) > MAX_FILE_BYTES:
        raise ValueError("attachment too large")
    return resp.content


def _docx_to_text(data: bytes) -> str:
    from docx import Document  # lazy: python-docx is optional at import time

    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def collect_thread_context(client: WebClient, token: str, channel: str, thread_ts: str) -> list[dict]:
    """Return Claude content blocks describing the candidate.

    Order matters for the model: files first (PDF resume, screenshots), then the
    thread text with author labels, then any bare URLs found (LinkedIn etc.),
    which we cannot fetch but the model should know exist.
    """
    result = client.conversations_replies(channel=channel, ts=thread_ts, limit=200)
    messages = result.get("messages", [])

    blocks: list[dict] = []
    text_parts: list[str] = []
    urls: list[str] = []

    for msg in messages:
        if msg.get("bot_id"):
            continue  # skip our own earlier replies
        raw = msg.get("text", "")
        cleaned = MENTION_RE.sub("", raw).strip()
        urls.extend(URL_RE.findall(raw))
        if cleaned and not GOLD_RE.fullmatch(cleaned.strip()):
            text_parts.append(f"[{msg.get('user', 'user')}] {cleaned}")

        for f in msg.get("files", []):
            mimetype = f.get("mimetype", "")
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            try:
                data = _download(url, token)
            except Exception as exc:  # network or size problems are not fatal
                log.warning("could not download %s: %s", f.get("name"), exc)
                continue
            if mimetype == "application/pdf":
                blocks.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf",
                               "data": base64.standard_b64encode(data).decode()},
                    "title": f.get("name", "resume.pdf"),
                })
            elif mimetype.startswith("image/") and mimetype in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mimetype,
                               "data": base64.standard_b64encode(data).decode()},
                })
            elif mimetype in TEXT_TYPES:
                text_parts.append(f"[file {f.get('name')}]\n{data.decode('utf-8', errors='replace')}")
            elif mimetype == DOCX_TYPE:
                try:
                    text_parts.append(f"[file {f.get('name')}]\n{_docx_to_text(data)}")
                except Exception as exc:
                    log.warning("docx parse failed for %s: %s", f.get("name"), exc)

    if text_parts:
        blocks.append({"type": "text", "text": "Slack thread:\n" + "\n\n".join(text_parts)})
    if urls:
        blocks.append({"type": "text", "text": "Links mentioned (not fetched): " + ", ".join(dict.fromkeys(urls))})
    return blocks


def post_blocks(client: WebClient, channel: str, thread_ts: str, blocks: list[dict], fallback_text: str) -> None:
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, blocks=blocks, text=fallback_text)


def post_error(client: WebClient, channel: str, thread_ts: str, message: str) -> None:
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":warning: {message}")
