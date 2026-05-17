"""
LINE Messaging API platform adapter for Hermes Agent.

A bundled platform plugin that runs an aiohttp webhook server, accepts LINE
webhook events (signature-verified), and relays messages to/from the agent
via the standard ``BasePlatformAdapter`` interface.

Design highlights
-----------------

**Reply token preferred, Push fallback.** LINE's reply token is single-use
and expires roughly 60 seconds after the inbound event. We try Reply first
(it's free) and fall back to the metered Push API when the token is absent,
expired, or rejected by the API.

**Slow-LLM postback button (optional).** When the LLM is still running past
``slow_response_threshold`` seconds (default 45, leaving 15s margin on the
60s reply-token TTL), we burn the original reply token to send a Template
Buttons bubble — the user taps it later to receive the cached answer via a
*fresh* reply token (also free). State machine: PENDING → READY → DELIVERED,
with ERROR for cancelled runs. Set the threshold to 0 to disable the
button and always Push-fallback instead.

**Three-allowlist gating.** Separate allowlists for users (U-prefixed),
groups (C-prefixed), and rooms (R-prefixed). ``LINE_ALLOW_ALL_USERS=true``
is a dev-only escape hatch.

**Media via public HTTPS.** LINE's Messaging API does *not* accept
binary uploads — images, audio, and video must be reachable HTTPS URLs.
We register registered tempfiles under ``/line/media/<token>/<filename>``
served by the same aiohttp app, with an allowed-roots traversal guard.
``LINE_PUBLIC_URL`` (e.g. ``https://my-tunnel.example.com``) overrides
the host:port construction so URLs are reachable when bind is 0.0.0.0
or behind a reverse proxy.

**5-message batching.** LINE accepts at most 5 message objects per
Reply/Push call; longer responses are smart-chunked at 4500 chars
(LINE per-bubble limit is 5000) and batched.

Synthesis credits
-----------------

This file is a synthesis of seven open community PRs adding LINE support
to Hermes Agent. It deliberately ports the *strongest* idea from each into
a single plugin-form module that requires zero core edits:

* PR #18153 (leepoweii)   — Template Buttons postback cache state machine,
  Markdown URL preservation, system-message bypass.
* PR #8398  (yuga-hashimoto) — media URL serving with traversal guard,
  send_voice / send_video, ``LINE_PUBLIC_URL`` env, macOS ``/tmp`` root.
* PR #16832 (jethac)      — config wiring style, voice/image tests.
* PR #21023 (perng)       — plugin-form skeleton (the only one already
  modeled on ``ADDING_A_PLATFORM.md``), reply→push fallback at 50s TTL,
  loading-animation indicator, source dispatcher.
* PR #14942 (soichiyo)    — Cloudflare-tunnel operating model (docs only).
* PR #14988 (David-0x221Eight) — text-first scope discipline.
* PR #6676  (liyoungc)    — Push-only mode (used as the ``threshold=0``
  fallback path here).
"""

from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote as _urlquote

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy / function-level imports for gateway internals are NOT used here —
# the plugin discovery flow imports adapter.py late enough that gateway is
# already loaded.
# ---------------------------------------------------------------------------

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
)
from gateway.config import Platform
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"
LINE_CONTENT_URL_FMT = "https://api-data.line.me/v2/bot/message/{message_id}/content"
LINE_BOT_INFO_URL = "https://api.line.me/v2/bot/info"

# LINE Messaging API hard limits
LINE_PER_BUBBLE_CHARS = 5000  # Hard limit per text message object
LINE_SAFE_BUBBLE_CHARS = 4500  # Conservative limit for chunking
LINE_MAX_MESSAGES_PER_CALL = 5  # API rejects >5 messages per Reply/Push
LINE_REPLY_TOKEN_TTL_SECONDS = 50  # Conservative cap below LINE's ~60s

# Webhook hardening
WEBHOOK_BODY_MAX_BYTES = 1_048_576  # 1 MiB — webhooks are tiny JSON
DEFAULT_WEBHOOK_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/line/webhook"
DEFAULT_MEDIA_PATH_PREFIX = "/line/media"

# Slow-LLM postback button defaults
DEFAULT_SLOW_RESPONSE_THRESHOLD = 45.0  # seconds; 0 disables
DEFAULT_PENDING_REPLY_TEXT = (
    "🤔 Still thinking. Tap below to fetch the answer when it's ready."
)
DEFAULT_BUTTON_LABEL = "Get answer"
DEFAULT_DELIVERED_TEXT = "Already replied ✅"
DEFAULT_INTERRUPTED_TEXT = "Run was interrupted before completion."

# Media defaults
MEDIA_TOKEN_TTL_SECONDS = 1800  # 30 minutes; LINE caches the URL aggressively
LINE_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per LINE docs
LINE_AV_MAX_BYTES = 200 * 1024 * 1024  # 200 MB for voice/video
DEFAULT_GROUP_MEDIA_CONTEXT_TTL_SECONDS = 0.0  # opt-in; preserves old behavior
SENT_MESSAGE_ID_TTL_SECONDS = 86400  # lets quoted replies address the bot for 1 day

GROUP_REPLY_MODES: Set[str] = {
    "always",
    "never",
    "mention_only",
    "mention_or_keyword",
    "keyword_only",
    "mention_keyword_or_chime",
    "chime_only",
}

DEFAULT_GROUP_CHIME_IN_COOLDOWN_SECONDS = 300
DEFAULT_GROUP_CHIME_IN_MIN_CHARS = 4
DEFAULT_GROUP_CHIME_IN_MAX_PER_DAY = 12
DEFAULT_GROUP_CHIME_IN_MIN_SCORE = 3
DEFAULT_GROUP_CHIME_IN_RECENT_REPLY_COOLDOWN_SECONDS = 0
DEFAULT_GROUP_CHIME_IN_HIGH_PRIORITY_SCORE = 6
DEFAULT_GROUP_CHIME_IN_BURST_WINDOW_SECONDS = 180
DEFAULT_GROUP_CHIME_IN_BURST_MESSAGE_COUNT = 16

GROUP_CHIME_QUESTION_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"[?？]",
        r"怎麼看",
        r"怎麼辦",
        r"如何",
        r"要不要",
        r"好不好",
        r"可以嗎",
        r"行嗎",
        r"會不會",
        r"有沒有",
        r"推薦",
        r"建議",
        r"哪個",
        r"選哪",
        r"覺得",
        r"適合",
        r"該不該",
    )
)

GROUP_CHIME_ASSISTANT_TASK_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"幫我",
        r"幫忙",
        r"麻煩",
        r"請幫",
        r"可以幫",
        r"看一下",
        r"看這",
        r"查一下",
        r"查查看",
        r"算一下",
        r"整理",
        r"摘要",
        r"分析",
        r"判斷",
        r"翻譯",
        r"轉成",
        r"弄成",
        r"做成",
    )
)

GROUP_CHIME_GOLF_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"成績",
        r"讓桿",
        r"總桿",
        r"淨桿",
        r"排名",
        r"誰贏",
        r"桿數",
        r"球局",
    )
)

GROUP_CHIME_GROUP_ASK_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"大家覺得",
        r"你們覺得",
        r"各位",
        r"有人知道",
        r"有人推薦",
        r"問一下",
        r"請教一下",
        r"求推薦",
        r"求救",
        r"有人用過",
        r"哪個比較",
    )
)

GROUP_CHIME_COORDINATION_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"誰要",
        r"有沒有人要",
        r"要不要約",
        r"一起",
        r"幾點",
        r"約嗎",
        r"集合",
        r"哪天",
        r"誰方便",
        r"誰有空",
        r"要訂嗎",
    )
)

GROUP_CHIME_SUPPORT_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"好累",
        r"好煩",
        r"崩潰",
        r"睡不著",
        r"不舒服",
        r"焦慮",
        r"壓力",
        r"難過",
        r"心情不好",
        r"快瘋",
    )
)

GROUP_CHIME_SINGULAR_DIRECTED_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(你|妳)(?!們)",
        r"(你|妳)覺得",
        r"(你|妳)要不要",
        r"(你|妳)可以",
        r"(你|妳)呢",
        r"跟你說",
        r"問你",
    )
)

GROUP_CHIME_SELF_OPINION_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^我覺得",
        r"^我比較想",
        r"^我偏向",
        r"^我會選",
        r"^我選",
    )
)

GROUP_CHIME_STATUS_UPDATE_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(我(先|剛|等等|等下|去|到了|到家|下班|睡了|出門|回來|吃飽|洗澡)|已經|收到|報告一下|更新一下|我決定)",
        r"(我先|我去|我到了|我下班了|我先睡|我先忙|我先撤)",
        r"(已寄出|已送出|已到|已處理)",
    )
)

GROUP_CHIME_LOW_SIGNAL_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(早安|午安|晚安|安安|收到|好喔|好哦|ok|okay|感謝|謝啦|謝謝|哈哈+|呵呵+|lol|www|讚|推|yes|no)[!！.。 ]*$",
        r"^[\W_ 😂🤣😆😅🙂🙃😉👍🙏❤️❤💯🔥]+$",
    )
)

# A 1×1 transparent PNG used as fallback video preview thumbnail when no
# explicit preview is supplied — LINE requires ``previewImageUrl`` for
# video messages. Sourced from the Python stdlib (no Pillow dependency).
_FALLBACK_PNG_PREVIEW = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000100377a7ff20000000049454e"
    "44ae426082"
)


# ---------------------------------------------------------------------------
# Markdown stripping (URL-preserving)
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_CODE_INLINE_RE = re.compile(r"`([^`]+)`")
_MD_FENCE_LINE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*$")
_MD_BULLET_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_LINE_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LINE_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_LINE_COPY_LINE_WIDTH = 120


def _strip_inline_markdown_for_line(text: str) -> str:
    """Remove inline Markdown while keeping the readable text and bare URLs."""
    text = _MD_CODE_INLINE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITAL_RE.sub(r"\1", text)
    return text


def _split_table_row_for_line(line: str) -> List[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [_strip_inline_markdown_for_line(cell.strip()) for cell in row.split("|")]


def _looks_like_table_row_for_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and "|" in stripped[1:-1]


def _rewrite_table_block_for_line(lines: List[str]) -> List[str]:
    """Convert a Markdown table into compact LINE-friendly bullet rows."""
    if len(lines) < 2:
        return lines
    headers = _split_table_row_for_line(lines[0])
    body_rows = [
        _split_table_row_for_line(line)
        for line in lines[2:]
        if line.strip() and not _LINE_TABLE_RULE_RE.match(line.strip())
    ]
    if not headers or not body_rows:
        return lines

    formatted: List[str] = []
    for row in body_rows:
        pairs: List[Tuple[str, str]] = []
        for idx, header in enumerate(headers):
            if idx >= len(row):
                break
            label = header or f"Column {idx + 1}"
            value = row[idx].strip()
            if value:
                pairs.append((label, value))
        if not pairs:
            continue
        if len(pairs) == 1:
            label, value = pairs[0]
            formatted.append(f"• {label}: {value}")
            continue
        if len(pairs) == 2:
            label, value = pairs[0]
            other_label, other_value = pairs[1]
            formatted.append(f"• {label}: {value}")
            formatted.append(f"  {other_label}: {other_value}")
            continue
        formatted.append("• " + " | ".join(f"{label}: {value}" for label, value in pairs))
    return formatted or lines


def _style_line_heading(line: str) -> Optional[str]:
    match = _LINE_HEADER_RE.match(line)
    if not match:
        return None
    title = _strip_inline_markdown_for_line(match.group(2).strip())
    if not title:
        return ""
    return f"【{title}】"


def _normalize_line_blank_runs(text: str) -> str:
    result: List[str] = []
    blank_run = 0
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue
        blank_run = 0
        result.append(line)
    return "\n".join(result).strip()


def _wrap_copy_friendly_lines_for_line(text: str) -> str:
    if not text:
        return text
    wrapped: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if (
            len(line) <= _LINE_COPY_LINE_WIDTH
            or not stripped
            or line.startswith((" ", "\t"))
        ):
            wrapped.append(line)
            continue
        wrapped.extend(
            textwrap.wrap(
                line,
                width=_LINE_COPY_LINE_WIDTH,
                break_long_words=False,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=True,
            )
            or [line]
        )
    return "\n".join(wrapped).strip()


def strip_markdown_preserving_urls(text: str) -> str:
    """Format Markdown-ish agent output for LINE text bubbles.

    LINE's text bubble has zero Markdown support — bold, italics, code
    fences, headings, and bullet markers all render as literal characters.
    URLs *are* auto-linked by the client, but only when they appear bare
    (not inside ``[label](url)`` syntax). This converts ``[label](url)``
    to ``label (url)`` so the URL remains tappable.

    Instead of flattening everything, keep a WeChat-like visual hierarchy in
    plain text: headings become ``【Heading】``, bullets become ``•``, Markdown
    tables become compact key/value bullets, and long copy-heavy lines wrap at
    a stable width.

    Source: PR #18153 (leepoweii) — adapted to keep code-block content
    visible (LINE users frequently want command snippets to land as
    plain text, not be eaten by the fence).
    """
    if not text:
        return text

    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result: List[str] = []
    in_code_block = False
    idx = 0

    while idx < len(lines):
        line = lines[idx].rstrip()
        stripped = line.strip()

        if _MD_FENCE_LINE_RE.match(stripped):
            in_code_block = not in_code_block
            idx += 1
            continue

        if in_code_block:
            result.append(line)
            idx += 1
            continue

        if (
            idx + 1 < len(lines)
            and _looks_like_table_row_for_line(line)
            and _LINE_TABLE_RULE_RE.match(lines[idx + 1].strip())
        ):
            table_lines = [line, lines[idx + 1].rstrip()]
            idx += 2
            while idx < len(lines) and _looks_like_table_row_for_line(lines[idx]):
                table_lines.append(lines[idx].rstrip())
                idx += 1
            result.extend(_rewrite_table_block_for_line(table_lines))
            continue

        heading = _style_line_heading(line)
        if heading is not None:
            result.append(heading)
            idx += 1
            continue

        styled = _strip_inline_markdown_for_line(line)
        styled = _MD_BULLET_RE.sub("• ", styled)
        result.append(styled)
        idx += 1

    return _wrap_copy_friendly_lines_for_line(_normalize_line_blank_runs("\n".join(result)))


def split_for_line(text: str, max_chars: int = LINE_SAFE_BUBBLE_CHARS) -> List[str]:
    """Split ``text`` into LINE-sized bubbles, preferring paragraph/line breaks.

    Returns at most ``LINE_MAX_MESSAGES_PER_CALL`` chunks; longer text is
    truncated with an ellipsis on the final chunk to keep the response
    deliverable in a single Reply/Push call.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    remaining = text
    while remaining and len(chunks) < LINE_MAX_MESSAGES_PER_CALL:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            remaining = ""
            break
        # Try to break on the latest paragraph or newline within budget.
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < int(max_chars * 0.5):
            cut = remaining.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        # Truncate gracefully — caller already burned its 5-bubble budget.
        if chunks:
            tail = chunks[-1]
            if len(tail) > max_chars - 1:
                tail = tail[: max_chars - 1]
            chunks[-1] = tail.rstrip() + "…"
        else:
            chunks.append(remaining[: max_chars - 1] + "…")
    return chunks


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_line_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Verify a LINE webhook's ``X-Line-Signature`` header.

    LINE signs the *raw* request body with HMAC-SHA256 keyed by the
    channel secret, then base64-encodes the digest. Constant-time
    comparison defends against timing oracles.
    """
    if not signature or not channel_secret or body is None:
        return False
    try:
        digest = hmac.new(
            channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
    except Exception:
        return False
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Cache state machine — slow-LLM postback flow
# ---------------------------------------------------------------------------

class State(enum.Enum):
    PENDING = "pending"  # button sent, LLM still running
    READY = "ready"      # LLM done, response cached, waiting for postback tap
    DELIVERED = "delivered"
    ERROR = "error"      # LLM raised / interrupted; cached error text waiting


@dataclass
class _CacheEntry:
    state: State
    payload: Any = None
    chat_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class RequestCache:
    """In-memory cache for slow-LLM postback retrieval.

    PRs #18153 originally combined two TTLs — one for PENDING (24h) and
    a shorter one for READY/DELIVERED/ERROR (1h). We keep the same model
    here.
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        pending_ttl_seconds: int = 86400,
    ) -> None:
        self._entries: Dict[str, _CacheEntry] = {}
        self._ttl = ttl_seconds
        self._pending_ttl = pending_ttl_seconds

    def register_pending(self, chat_id: str) -> str:
        rid = str(uuid.uuid4())
        self._entries[rid] = _CacheEntry(state=State.PENDING, chat_id=chat_id)
        return rid

    def get(self, request_id: str) -> Optional[_CacheEntry]:
        return self._entries.get(request_id)

    def set_ready(self, request_id: str, payload: Any) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return
        entry.state = State.READY
        entry.payload = payload
        entry.updated_at = time.time()

    def set_error(self, request_id: str, message: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state is not State.PENDING:
            return
        entry.state = State.ERROR
        entry.payload = message
        entry.updated_at = time.time()

    def mark_delivered(self, request_id: str) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.state not in (State.READY, State.ERROR):
            return
        entry.state = State.DELIVERED
        entry.updated_at = time.time()

    def find_pending_for_chat(self, chat_id: str) -> Optional[str]:
        for rid, entry in self._entries.items():
            if entry.state is State.PENDING and entry.chat_id == chat_id:
                return rid
        return None

    def prune(self) -> int:
        now = time.time()
        removed = 0
        for rid in list(self._entries.keys()):
            entry = self._entries[rid]
            if entry.state is State.PENDING:
                if now - entry.created_at > self._pending_ttl:
                    del self._entries[rid]
                    removed += 1
            else:
                if now - entry.updated_at > self._ttl:
                    del self._entries[rid]
                    removed += 1
        return removed


# ---------------------------------------------------------------------------
# Inbound dedup
# ---------------------------------------------------------------------------

class _MessageDeduplicator:
    """Bounded LRU of LINE webhook event IDs to ignore at-least-once retries."""

    def __init__(self, max_size: int = 1000) -> None:
        self._seen: Dict[str, float] = {}
        self._max = max_size

    def is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        if len(self._seen) >= self._max:
            # Drop the oldest 10% so we don't trim on every insert.
            cutoff = sorted(self._seen.values())[len(self._seen) // 10 or 1]
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._seen[event_id] = time.time()
        return False


# ---------------------------------------------------------------------------
# Source / chat-id resolution
# ---------------------------------------------------------------------------

def _resolve_chat(source: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(chat_id, chat_type)`` from a LINE event ``source`` block.

    LINE sources are one of:
      * ``{"type": "user",  "userId":  "U..."}``  → 1:1 DM
      * ``{"type": "group", "groupId": "C...", "userId": "U..."}``  → group chat
      * ``{"type": "room",  "roomId":  "R...", "userId": "U..."}``  → multi-user room

    Source: PR #21023 (perng), unchanged.
    """
    src_type = (source or {}).get("type", "")
    if src_type == "group":
        return source.get("groupId", ""), "group"
    if src_type == "room":
        return source.get("roomId", ""), "room"
    if src_type == "user":
        return source.get("userId", ""), "dm"
    return "", "dm"


def _allowed_for_source(
    source: Dict[str, Any],
    *,
    allow_all: bool,
    user_ids: Set[str],
    group_ids: Set[str],
    room_ids: Set[str],
) -> bool:
    """Three-list gate — credit PR #18153."""
    if allow_all:
        return True
    src_type = (source or {}).get("type", "")
    if src_type == "user":
        uid = source.get("userId", "")
        return bool(uid) and uid in user_ids
    if src_type == "group":
        gid = source.get("groupId", "")
        return bool(gid) and gid in group_ids
    if src_type == "room":
        rid = source.get("roomId", "")
        return bool(rid) and rid in room_ids
    return False


def _message_mentions_self(message: Optional[Dict[str, Any]]) -> bool:
    """Return true when LINE's native mention metadata targets this bot."""
    mention = (message or {}).get("mention") or {}
    for mentionee in mention.get("mentionees", []) or []:
        if mentionee.get("isSelf") or str(mentionee.get("type", "")).lower() == "bot":
            return True
    return False


def _find_group_trigger_keyword(text: str, keywords: List[str]) -> Optional[str]:
    clean_text = (text or "").strip()
    if not clean_text:
        return None
    for keyword in keywords:
        clean_keyword = (keyword or "").strip()
        if not clean_keyword:
            continue
        if clean_keyword.isascii():
            pattern = (
                rf"(?<![A-Za-z0-9_]){re.escape(clean_keyword)}"
                rf"(?:(?![A-Za-z0-9_])|(?=[\u4e00-\u9fff]))"
            )
            if re.search(pattern, clean_text, flags=re.IGNORECASE):
                return clean_keyword
        elif clean_keyword in clean_text:
            return clean_keyword
    return None


def _group_trigger_allowed(
    text: str,
    keywords: List[str],
    message: Optional[Dict[str, Any]] = None,
    *,
    quoted_sent_message: bool = False,
    chime_allowed: bool = False,
    mode: str = "mention_or_keyword",
) -> bool:
    """Return true when a group message should be handled.

    An empty keyword list preserves historical behavior for existing LINE
    profiles. Profiles that opt in with LINE_GROUP_TRIGGER_KEYWORDS or
    LINE_GROUP_REPLY_MODE can safely join busy groups without replying to every
    message.
    """
    clean_mode = (mode or "mention_or_keyword").strip().lower()
    if clean_mode not in GROUP_REPLY_MODES:
        clean_mode = "mention_or_keyword"
    if clean_mode == "never":
        return False
    if clean_mode == "always":
        return True
    if quoted_sent_message:
        return True
    if _message_mentions_self(message):
        return True
    if clean_mode == "mention_only":
        return False
    if not keywords:
        return chime_allowed and clean_mode in {"mention_keyword_or_chime", "chime_only"}
    if clean_mode in {"mention_or_keyword", "keyword_only", "mention_keyword_or_chime"}:
        if bool(_find_group_trigger_keyword(text, keywords)):
            return True
    if clean_mode in {"mention_keyword_or_chime", "chime_only"}:
        return chime_allowed
    return False


def _group_chime_score(text: str, *, has_recent_media: bool = False, has_quote: bool = False) -> Tuple[int, List[str]]:
    clean_text = (text or "").strip()
    if not clean_text:
        return -99, ["empty"]

    score = 0
    reasons: List[str] = []
    positive_reasons: Set[str] = set()

    for pattern in GROUP_CHIME_QUESTION_PATTERNS:
        if pattern.search(clean_text):
            score += 3
            reasons.append("question")
            positive_reasons.add("question")
            break
    for pattern in GROUP_CHIME_ASSISTANT_TASK_PATTERNS:
        if pattern.search(clean_text):
            score += 4
            reasons.append("assistant_task")
            positive_reasons.add("assistant_task")
            break
    for pattern in GROUP_CHIME_GOLF_PATTERNS:
        if pattern.search(clean_text):
            score += 2
            reasons.append("golf_context")
            positive_reasons.add("golf_context")
            break
    for label, patterns in (
        ("group_ask", GROUP_CHIME_GROUP_ASK_PATTERNS),
        ("coordination", GROUP_CHIME_COORDINATION_PATTERNS),
        ("support", GROUP_CHIME_SUPPORT_PATTERNS),
    ):
        for pattern in patterns:
            if pattern.search(clean_text):
                score += 3
                reasons.append(label)
                positive_reasons.add(label)
                break

    if has_recent_media:
        score += 2
        reasons.append("recent_media")
    if has_quote:
        score += 2
        reasons.append("quote")
    if len(clean_text) >= 24:
        score += 1
        reasons.append("longer_context")
    if len(clean_text) >= 60 and positive_reasons:
        score += 1
        reasons.append("rich_context")

    for pattern in GROUP_CHIME_LOW_SIGNAL_PATTERNS:
        if pattern.fullmatch(clean_text):
            score -= 4
            reasons.append("low_signal")
            break
    for pattern in GROUP_CHIME_SINGULAR_DIRECTED_PATTERNS:
        if pattern.search(clean_text) and "group_ask" not in positive_reasons:
            score -= 2
            reasons.append("single_person_directed")
            break
    for pattern in GROUP_CHIME_SELF_OPINION_PATTERNS:
        if pattern.search(clean_text) and not {"group_ask", "coordination"} & positive_reasons:
            score -= 2
            reasons.append("self_opinion")
            break
    for pattern in GROUP_CHIME_STATUS_UPDATE_PATTERNS:
        if pattern.search(clean_text) and not positive_reasons:
            score -= 2
            reasons.append("status_update")
            break

    if not positive_reasons and re.search(r"[。.]$", clean_text):
        score -= 1
        reasons.append("closed_statement")

    return score, reasons


def _strip_leading_group_trigger(text: str, keywords: List[str]) -> str:
    """Remove a leading mention/keyword before handing text to the agent."""
    if not text or not keywords:
        return text
    stripped = text.lstrip()
    for keyword in sorted(keywords, key=len, reverse=True):
        if stripped.casefold().startswith(keyword.casefold()):
            remainder = stripped[len(keyword):].lstrip(" \t:：,，")
            return remainder or text
    return text


def _line_message_type(msg_type: str, text: str = "") -> MessageType:
    """Map LINE webhook message types to Hermes' normalized MessageType enum."""
    if msg_type == "text":
        return MessageType.COMMAND if (text or "").startswith("/") else MessageType.TEXT
    return {
        "image": MessageType.PHOTO,
        "audio": MessageType.AUDIO,
        "video": MessageType.VIDEO,
        "file": MessageType.DOCUMENT,
        "sticker": MessageType.STICKER,
        "location": MessageType.LOCATION,
    }.get(msg_type, MessageType.TEXT)


def _message_type_for_attached_media(media_types: List[str]) -> MessageType:
    for media_type in media_types:
        if media_type.startswith("image/"):
            return MessageType.PHOTO
        if media_type.startswith("video/"):
            return MessageType.VIDEO
        if media_type.startswith("audio/"):
            return MessageType.AUDIO
    return MessageType.DOCUMENT


def _line_media_type(msg_type: str, filename: str = "") -> str:
    """Return a MIME-ish type for LINE media so gateway vision routing works."""
    if msg_type == "image":
        return "image/jpeg"
    if msg_type == "audio":
        return "audio/mp4"
    if msg_type == "video":
        return "video/mp4"
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return "application/octet-stream"


def _parse_sent_messages_body(body: str) -> List[Dict[str, Any]]:
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    sent = data.get("sentMessages") if isinstance(data, dict) else None
    if not isinstance(sent, list):
        return []
    return [item for item in sent if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# LINE Reply / Push HTTP client
# ---------------------------------------------------------------------------

class _LineClient:
    """Thin async wrapper around the LINE Messaging API.

    We use ``aiohttp`` directly to avoid a ``line-bot-sdk`` dependency
    (the SDK pulls in its own httpx pin and the ergonomic gain is small
    for the four endpoints we actually call).
    """

    def __init__(self, channel_access_token: str, *, timeout: float = 15.0) -> None:
        self._token = channel_access_token
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        }

    async def reply(self, reply_token: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                LINE_REPLY_URL,
                headers=self._headers,
                json={"replyToken": reply_token, "messages": messages},
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"LINE reply {resp.status}: {body[:200]}")
                return _parse_sent_messages_body(body)

    async def push(self, chat_id: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                LINE_PUSH_URL,
                headers=self._headers,
                json={"to": chat_id, "messages": messages},
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"LINE push {resp.status}: {body[:200]}")
                return _parse_sent_messages_body(body)

    async def loading(self, chat_id: str, seconds: int = 60) -> None:
        """Loading indicator (DM only). LINE rejects this for groups/rooms."""
        if not chat_id or not chat_id.startswith("U"):
            return
        import aiohttp
        # LINE caps loadingSeconds in 5-step increments, max 60.
        clamped = max(5, min(60, (seconds // 5) * 5 or 5))
        try:
            timeout = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await session.post(
                    LINE_LOADING_URL,
                    headers=self._headers,
                    json={"chatId": chat_id, "loadingSeconds": clamped},
                )
        except Exception as exc:  # best-effort; never raise
            logger.debug("LINE loading indicator failed: %s", exc)

    async def fetch_content(self, message_id: str) -> bytes:
        """Download an inbound media message's binary content."""
        import aiohttp
        url = LINE_CONTENT_URL_FMT.format(message_id=message_id)
        timeout = aiohttp.ClientTimeout(total=30.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Authorization": f"Bearer {self._token}"}) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"LINE content {resp.status}")
                return await resp.read()

    async def get_bot_user_id(self) -> Optional[str]:
        """Fetch this channel's own userId so we can filter self-messages."""
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(LINE_BOT_INFO_URL, headers=self._headers) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
                    return data.get("userId")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _text_message(text: str) -> Dict[str, Any]:
    """Build a LINE text message object, capped to per-bubble max."""
    if len(text) > LINE_PER_BUBBLE_CHARS:
        text = text[: LINE_PER_BUBBLE_CHARS - 1] + "…"
    return {"type": "text", "text": text}


def _image_message(original_url: str, preview_url: Optional[str] = None) -> Dict[str, Any]:
    return {
        "type": "image",
        "originalContentUrl": original_url,
        "previewImageUrl": preview_url or original_url,
    }


def _audio_message(url: str, duration_ms: int = 1000) -> Dict[str, Any]:
    return {
        "type": "audio",
        "originalContentUrl": url,
        "duration": int(duration_ms),
    }


def _video_message(url: str, preview_url: str) -> Dict[str, Any]:
    return {
        "type": "video",
        "originalContentUrl": url,
        "previewImageUrl": preview_url,
    }


def build_postback_button_message(
    text: str, button_label: str, request_id: str
) -> Dict[str, Any]:
    """Template Buttons message — the slow-LLM postback bubble.

    From PR #18153 (leepoweii). Template Buttons stay tappable from chat
    history, unlike Quick Reply chips which are dismissed the moment any
    new message arrives in the chat.

    LINE limits: ``text`` ≤ 160 chars, ``altText`` ≤ 400 chars.
    """
    truncated = text if len(text) <= 160 else text[:157] + "..."
    alt = text if len(text) <= 400 else text[:397] + "..."
    return {
        "type": "template",
        "altText": alt,
        "template": {
            "type": "buttons",
            "text": truncated,
            "actions": [
                {
                    "type": "postback",
                    "label": button_label[:20] or "Get answer",
                    "data": json.dumps(
                        {"action": "show_response", "request_id": request_id}
                    ),
                    "displayText": button_label[:300] or "Get answer",
                }
            ],
        },
    }


# Prefixes the gateway uses for system busy-acks (interrupting / queued /
# steered). When the postback cache has a PENDING entry we *bypass* the
# cache for these so they reach the user as visible bubbles instead of
# being silently swallowed. From PR #18153.
_SYSTEM_BYPASS_PREFIXES: Tuple[str, ...] = (
    "⚡ Interrupting",
    "⏳ Queued",
    "⏩ Steered",
)


def _is_system_bypass(content: str) -> bool:
    if not content:
        return False
    return any(content.startswith(p) for p in _SYSTEM_BYPASS_PREFIXES)


def _is_internal_notice(content: str) -> bool:
    """Internal maintenance messages should stay in logs, not LINE chats."""
    if not content:
        return False
    stripped = content.strip()
    return (
        stripped.startswith("💾")
        or stripped.startswith("Self-improvement review:")
        or "Self-improvement review:" in stripped
    )


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _csv_set(value: str) -> Set[str]:
    if not value:
        return set()
    return {x.strip() for x in value.split(",") if x.strip()}


def _csv_list(value: str) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        key = value.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _truthy_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int_env_or_extra(name: str, extra: Dict[str, Any], key: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        raw = extra.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class LineAdapter(BasePlatformAdapter):
    """LINE Messaging API gateway adapter."""

    # LINE has its own message-edit story (none) — we always send fresh
    # bubbles, never edit, so REQUIRES_EDIT_FINALIZE stays False.

    def __init__(self, config, **kwargs):
        platform = Platform("line")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Credentials
        self.channel_access_token = (
            os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
            or extra.get("channel_access_token", "")
        )
        self.channel_secret = (
            os.getenv("LINE_CHANNEL_SECRET")
            or extra.get("channel_secret", "")
        )

        # Webhook server
        self.webhook_host = os.getenv("LINE_HOST") or extra.get("host", "0.0.0.0")
        try:
            self.webhook_port = int(
                os.getenv("LINE_PORT") or extra.get("port", DEFAULT_WEBHOOK_PORT)
            )
        except (TypeError, ValueError):
            self.webhook_port = DEFAULT_WEBHOOK_PORT
        self.webhook_path = extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)

        # Public base URL — required for media sending when bind isn't
        # publicly reachable.
        self.public_base_url = (
            os.getenv("LINE_PUBLIC_URL")
            or extra.get("public_url", "")
            or ""
        ).rstrip("/")

        # Three-allowlist gating
        self.allow_all = _truthy_env(
            "LINE_ALLOW_ALL_USERS", bool(extra.get("allow_all_users", False))
        )
        self.allowed_users = _csv_set(
            os.getenv("LINE_ALLOWED_USERS", "")
        ) | set(extra.get("allowed_users", []))
        self.allowed_groups = _csv_set(
            os.getenv("LINE_ALLOWED_GROUPS", "")
        ) | set(extra.get("allowed_groups", []))
        self.allowed_rooms = _csv_set(
            os.getenv("LINE_ALLOWED_ROOMS", "")
        ) | set(extra.get("allowed_rooms", []))
        self.group_trigger_keywords = _dedupe_preserve_order(
            _csv_list(os.getenv("LINE_GROUP_TRIGGER_KEYWORDS", ""))
            + list(extra.get("group_trigger_keywords", []))
        )
        default_group_reply_mode = (
            "mention_or_keyword" if self.group_trigger_keywords else "always"
        )
        self.group_reply_mode = (
            os.getenv("LINE_GROUP_REPLY_MODE")
            or extra.get("group_reply_mode")
            or default_group_reply_mode
        ).strip().lower()
        if self.group_reply_mode not in GROUP_REPLY_MODES:
            self.group_reply_mode = default_group_reply_mode
        try:
            self.group_media_context_ttl = float(
                os.getenv("LINE_GROUP_MEDIA_CONTEXT_TTL_SECONDS")
                or extra.get(
                    "group_media_context_ttl_seconds",
                    DEFAULT_GROUP_MEDIA_CONTEXT_TTL_SECONDS,
                )
            )
        except (TypeError, ValueError):
            self.group_media_context_ttl = DEFAULT_GROUP_MEDIA_CONTEXT_TTL_SECONDS
        self.group_chime_in_enabled = _truthy_env(
            "LINE_GROUP_CHIME_IN_ENABLED",
            bool(extra.get("group_chime_in_enabled", False)),
        )
        self.group_chime_in_cooldown_seconds = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_COOLDOWN_SECONDS",
            extra,
            "group_chime_in_cooldown_seconds",
            DEFAULT_GROUP_CHIME_IN_COOLDOWN_SECONDS,
        )
        self.group_chime_in_min_chars = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_MIN_CHARS",
            extra,
            "group_chime_in_min_chars",
            DEFAULT_GROUP_CHIME_IN_MIN_CHARS,
        )
        self.group_chime_in_max_per_day = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_MAX_PER_DAY",
            extra,
            "group_chime_in_max_per_day",
            DEFAULT_GROUP_CHIME_IN_MAX_PER_DAY,
        )
        self.group_chime_in_min_score = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_MIN_SCORE",
            extra,
            "group_chime_in_min_score",
            DEFAULT_GROUP_CHIME_IN_MIN_SCORE,
        )
        self.group_chime_in_recent_reply_cooldown_seconds = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_RECENT_REPLY_COOLDOWN_SECONDS",
            extra,
            "group_chime_in_recent_reply_cooldown_seconds",
            DEFAULT_GROUP_CHIME_IN_RECENT_REPLY_COOLDOWN_SECONDS,
        )
        self.group_chime_in_high_priority_score = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_HIGH_PRIORITY_SCORE",
            extra,
            "group_chime_in_high_priority_score",
            DEFAULT_GROUP_CHIME_IN_HIGH_PRIORITY_SCORE,
        )
        self.group_chime_in_burst_window_seconds = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_BURST_WINDOW_SECONDS",
            extra,
            "group_chime_in_burst_window_seconds",
            DEFAULT_GROUP_CHIME_IN_BURST_WINDOW_SECONDS,
        )
        self.group_chime_in_burst_message_count = _int_env_or_extra(
            "LINE_GROUP_CHIME_IN_BURST_MESSAGE_COUNT",
            extra,
            "group_chime_in_burst_message_count",
            DEFAULT_GROUP_CHIME_IN_BURST_MESSAGE_COUNT,
        )
        self.group_chime_in_allowed_groups = _csv_set(
            os.getenv("LINE_GROUP_CHIME_IN_ALLOWED_GROUPS", "")
        ) | set(extra.get("group_chime_in_allowed_groups", []))

        # Slow-LLM postback button threshold
        try:
            self.slow_response_threshold = float(
                os.getenv("LINE_SLOW_RESPONSE_THRESHOLD")
                or extra.get("slow_response_threshold", DEFAULT_SLOW_RESPONSE_THRESHOLD)
            )
        except (TypeError, ValueError):
            self.slow_response_threshold = DEFAULT_SLOW_RESPONSE_THRESHOLD

        # User-overridable copy
        self.pending_text = (
            os.getenv("LINE_PENDING_TEXT")
            or extra.get("pending_text", DEFAULT_PENDING_REPLY_TEXT)
        )
        self.button_label = (
            os.getenv("LINE_BUTTON_LABEL")
            or extra.get("button_label", DEFAULT_BUTTON_LABEL)
        )
        self.delivered_text = (
            os.getenv("LINE_DELIVERED_TEXT")
            or extra.get("delivered_text", DEFAULT_DELIVERED_TEXT)
        )
        self.interrupted_text = (
            os.getenv("LINE_INTERRUPTED_TEXT")
            or extra.get("interrupted_text", DEFAULT_INTERRUPTED_TEXT)
        )

        # Runtime state
        self._client: Optional[_LineClient] = None
        self._app = None  # aiohttp.web.Application
        self._runner = None  # aiohttp.web.AppRunner
        self._site = None  # aiohttp.web.TCPSite
        self._reply_tokens: Dict[str, Tuple[str, float]] = {}  # chat_id → (token, expiry)
        self._cache = RequestCache()
        self._dedup = _MessageDeduplicator()
        self._bot_user_id: Optional[str] = None
        self._lock_key: Optional[str] = None

        # Media state
        self._media_tokens: Dict[str, Tuple[str, float]] = {}  # token → (path, expiry)
        self._media_temp_paths: Set[str] = set()
        self._media_ttl = MEDIA_TOKEN_TTL_SECONDS
        self._group_media_context: Dict[str, List[Tuple[float, str, str, str]]] = {}
        self._sent_message_ids: Dict[str, float] = {}
        self._sent_message_context: Dict[str, Tuple[float, str]] = {}
        self._sent_message_media_context: Dict[str, Tuple[float, str, str]] = {}
        self._group_chime_state: Dict[str, Dict[str, Any]] = {}
        self._group_recent_message_times: Dict[str, List[float]] = {}
        self._group_last_reply_at: Dict[str, float] = {}

        # Pending-button slot per chat — ensures one outstanding postback
        # button per chat at a time. Postback cache request_id keyed by chat_id.
        self._pending_buttons: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not self.channel_access_token or not self.channel_secret:
            self._set_fatal_error(
                "config_missing",
                "LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET must be set",
                retryable=False,
            )
            return False

        # Prevent two profiles from running on the same channel access token.
        try:
            from gateway.status import acquire_scoped_lock
            # Use a hash of the token so we don't write the secret to disk.
            tok_hash = hashlib.sha256(self.channel_access_token.encode()).hexdigest()[:16]
            if not acquire_scoped_lock("line", tok_hash):
                self._set_fatal_error(
                    "lock_conflict",
                    "LINE channel already in use by another profile",
                    retryable=False,
                )
                return False
            self._lock_key = tok_hash
        except ImportError:
            self._lock_key = None

        self._client = _LineClient(self.channel_access_token)

        # Best-effort: fetch our own bot userId for self-message filtering.
        # If the call fails (offline tests, transient 5xx) we fall back to
        # not filtering self-events; the cost is minor (LINE doesn't
        # actually echo our own messages back).
        try:
            self._bot_user_id = await self._client.get_bot_user_id()
        except Exception as exc:
            logger.debug("LINE: get_bot_user_id failed: %s", exc)
            self._bot_user_id = None

        # Spin up the aiohttp webhook server.
        try:
            from aiohttp import web
        except ImportError:
            self._set_fatal_error(
                "missing_dep",
                "aiohttp is required for the LINE adapter — install with `pip install aiohttp`",
                retryable=False,
            )
            return False

        self._app = web.Application(client_max_size=WEBHOOK_BODY_MAX_BYTES)
        self._app.router.add_post(self.webhook_path, self._handle_webhook)
        # Public health probe — useful for tunnel/proxy verification.
        self._app.router.add_get(f"{self.webhook_path}/health", self._handle_health)
        # Media serving endpoint.
        self._app.router.add_get(
            f"{DEFAULT_MEDIA_PATH_PREFIX}/{{token}}/{{filename}}",
            self._handle_media,
        )

        self._runner = web.AppRunner(self._app)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
            await self._site.start()
        except OSError as exc:
            self._set_fatal_error(
                "bind_failed",
                f"Could not bind LINE webhook on {self.webhook_host}:{self.webhook_port}: {exc}",
                retryable=True,
            )
            return False

        self._mark_connected()
        logger.info(
            "LINE: webhook listening on %s:%s%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
            f" (public: {self.public_base_url})" if self.public_base_url else "",
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        self._app = None

        # Cleanup any tracked tempfiles.
        for path in list(self._media_temp_paths):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._media_temp_paths.clear()
        self._media_tokens.clear()
        self._group_media_context.clear()
        self._sent_message_ids.clear()
        self._sent_message_context.clear()
        self._sent_message_media_context.clear()
        self._group_chime_state.clear()
        self._group_recent_message_times.clear()
        self._group_last_reply_at.clear()

        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("line", self._lock_key)
            except Exception:
                pass
            self._lock_key = None

    # ------------------------------------------------------------------
    # Webhook handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request) -> Any:
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "line"})

    async def _handle_webhook(self, request) -> Any:
        from aiohttp import web

        # Body cap defends against memory-exhaustion via crafted Content-Length
        # (aiohttp's client_max_size only applies to certain body modes).
        try:
            body = await request.read()
        except Exception as exc:
            logger.debug("LINE: read failed: %s", exc)
            return web.Response(status=400, text="bad request")
        if len(body) > WEBHOOK_BODY_MAX_BYTES:
            return web.Response(status=413, text="payload too large")

        signature = request.headers.get("X-Line-Signature", "")
        if not verify_line_signature(body, signature, self.channel_secret):
            return web.Response(status=401, text="invalid signature")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.Response(status=400, text="bad json")

        events = payload.get("events", []) or []
        for event in events:
            try:
                await self._dispatch_event(event)
            except Exception:
                logger.exception("LINE: dispatch_event failed")

        return web.Response(status=200, text="ok")

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")
        source = event.get("source") or {}
        webhook_event_id = event.get("webhookEventId", "") or ""

        # Dedup retries (LINE webhooks may be re-delivered).
        if webhook_event_id and self._dedup.is_duplicate(webhook_event_id):
            logger.debug("LINE: ignoring duplicate webhook event %s", webhook_event_id)
            return

        # Filter our own messages (self-echo).
        sender_user_id = source.get("userId", "")
        if self._bot_user_id and sender_user_id == self._bot_user_id:
            return

        # Allowlist gate.
        if not _allowed_for_source(
            source,
            allow_all=self.allow_all,
            user_ids=self.allowed_users,
            group_ids=self.allowed_groups,
            room_ids=self.allowed_rooms,
        ):
            logger.info("LINE: rejecting unauthorized source %s", source)
            return

        if event_type == "message":
            await self._handle_message_event(event)
        elif event_type == "postback":
            await self._handle_postback_event(event)
        elif event_type in ("follow", "unfollow", "join", "leave"):
            logger.info("LINE: lifecycle event %s from %s", event_type, source)
        else:
            logger.debug("LINE: ignoring event type %r", event_type)

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        msg = event.get("message") or {}
        msg_type = msg.get("type", "")
        message_id = msg.get("id", "")
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}
        chat_id, chat_type = _resolve_chat(source)
        user_id = source.get("userId", "") or chat_id
        if chat_type == "group":
            self._mark_group_message_seen(chat_id)

        group_gate_enabled = self.group_reply_mode != "always"
        if chat_type == "group" and group_gate_enabled and msg_type != "text":
            if msg_type == "image" and self.group_media_context_ttl > 0:
                filename = msg.get("fileName", "") or ""
                local_path = await self._download_media(
                    message_id,
                    msg_type,
                    filename,
                    warn=False,
                )
                if local_path:
                    media_type = _line_media_type(msg_type, filename or local_path)
                    self._remember_group_media(chat_id, user_id, local_path, media_type)
                    logger.info("LINE: cached group %s context from %s", msg_type, chat_id)
                else:
                    logger.info("LINE: ignoring group %s message without text trigger from %s", msg_type, chat_id)
            else:
                logger.info("LINE: ignoring group %s message without text trigger from %s", msg_type, chat_id)
            return

        # Stash the reply token for outbound use.
        if chat_id and reply_token:
            self._reply_tokens[chat_id] = (
                reply_token,
                time.time() + LINE_REPLY_TOKEN_TTL_SECONDS,
            )

        # Handle media inbound — fetch the binary, cache it, and surface a
        # vision-tool-friendly local path on the MessageEvent.
        media_urls: List[str] = []
        media_types: List[str] = []
        text = ""
        quoted_message_id = ""

        if msg_type == "text":
            text = msg.get("text", "") or ""
            quoted_message_id = msg.get("quotedMessageId", "") or ""
        elif msg_type in ("image", "audio", "video", "file"):
            filename = msg.get("fileName", "") or ""
            local_path = await self._download_media(message_id, msg_type, filename)
            if local_path:
                media_urls.append(local_path)
                media_types.append(_line_media_type(msg_type, filename or local_path))
            text = f"[{msg_type}]"
        elif msg_type == "sticker":
            keywords = msg.get("keywords") or []
            text = f"[sticker: {', '.join(keywords)}]" if keywords else "[sticker]"
        elif msg_type == "location":
            title = msg.get("title", "")
            address = msg.get("address", "")
            text = f"[location: {title} {address}]".strip()
        else:
            text = f"[unsupported message type: {msg_type}]"

        quoted_sent_message = self._is_replying_to_sent_message(msg)
        quoted_sent_text = self._sent_message_text_for_quote(msg)
        quoted_sent_media = self._sent_message_media_for_quote(msg)
        chime_allowed, chime_reason = (
            self._should_group_chime_in(chat_id, user_id, msg, text)
            if chat_type == "group"
            else (False, "")
        )
        if chat_type == "group" and not _group_trigger_allowed(
            text,
            self.group_trigger_keywords,
            msg,
            quoted_sent_message=quoted_sent_message,
            chime_allowed=chime_allowed,
            mode=self.group_reply_mode,
        ):
            logger.info("LINE: ignoring group message without trigger from %s", chat_id)
            return
        if chime_allowed:
            self._record_group_chime(chat_id, text, chime_reason)
            logger.info("LINE: allowing group chime-in from %s (%s)", chat_id, chime_reason)
        if chat_type == "group":
            text = _strip_leading_group_trigger(text, self.group_trigger_keywords)

        if quoted_sent_media and not media_urls:
            local_path, media_type = quoted_sent_media
            media_urls.append(local_path)
            media_types.append(media_type)
            logger.info("LINE: attached quoted outbound media for message %s", message_id)
        elif quoted_message_id and not media_urls:
            local_path = await self._download_media(
                quoted_message_id,
                "image",
                f"line_quoted_{quoted_message_id}.jpg",
                warn=False,
            )
            if local_path:
                media_urls.append(local_path)
                media_types.append(_line_media_type("image", local_path))
                logger.info("LINE: attached quoted image content for message %s", message_id)

        if chat_type == "group" and msg_type == "text" and not media_urls:
            recent_media = self._get_recent_group_media(chat_id, user_id)
            if recent_media:
                local_path, media_type = recent_media
                media_urls.append(local_path)
                media_types.append(media_type)
                logger.info("LINE: attached recent group media context from %s", chat_id)

        # Best-effort typing indicator (DM only).
        if chat_type == "dm" and self._client:
            asyncio.create_task(self._client.loading(chat_id))

        source_obj = self.build_source(
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_id,
            chat_name=chat_id,
        )

        event_obj = MessageEvent(
            text=text,
            message_type=(
                _message_type_for_attached_media(media_types)
                if media_urls and msg_type == "text"
                else _line_message_type(msg_type, text)
            ),
            source=source_obj,
            raw_message=event,
            message_id=message_id,
            reply_to_message_id=quoted_message_id or None,
            reply_to_text=quoted_sent_text or None,
            media_urls=media_urls,
            media_types=media_types,
        )

        await self.handle_message(event_obj)

    async def _handle_postback_event(self, event: Dict[str, Any]) -> None:
        """User tapped the slow-LLM postback button — deliver cached payload."""
        postback = event.get("postback") or {}
        data = postback.get("data", "") or ""
        reply_token = event.get("replyToken", "")
        source = event.get("source") or {}
        chat_id, _ = _resolve_chat(source)

        try:
            parsed = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return

        if parsed.get("action") != "show_response":
            return
        request_id = parsed.get("request_id", "")
        if not request_id:
            return

        entry = self._cache.get(request_id)
        if not self._client or not reply_token or not entry:
            return

        if entry.state is State.READY:
            payload = entry.payload or ""
            chunks = split_for_line(strip_markdown_preserving_urls(str(payload)))
            messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]
            try:
                self._remember_sent_messages(
                    await self._client.reply(reply_token, messages),
                    messages,
                    chat_id=chat_id,
                )
                self._cache.mark_delivered(request_id)
                self._pending_buttons.pop(chat_id, None)
            except Exception as exc:
                logger.warning("LINE: postback reply failed (%s); falling back to push", exc)
                try:
                    self._remember_sent_messages(
                        await self._client.push(chat_id, messages),
                        messages,
                        chat_id=chat_id,
                    )
                    self._cache.mark_delivered(request_id)
                    self._pending_buttons.pop(chat_id, None)
                except Exception as exc2:
                    logger.error("LINE: postback push fallback failed: %s", exc2)
        elif entry.state is State.ERROR:
            text = str(entry.payload or self.interrupted_text)
            try:
                messages = [_text_message(text)]
                self._remember_sent_messages(
                    await self._client.reply(reply_token, messages),
                    messages,
                    chat_id=chat_id,
                )
                self._cache.mark_delivered(request_id)
                self._pending_buttons.pop(chat_id, None)
            except Exception as exc:
                logger.warning("LINE: postback ERROR reply failed: %s", exc)
        elif entry.state is State.DELIVERED:
            try:
                messages = [_text_message(self.delivered_text)]
                self._remember_sent_messages(
                    await self._client.reply(reply_token, messages),
                    messages,
                    chat_id=chat_id,
                )
            except Exception:
                pass
        elif entry.state is State.PENDING:
            # Still working — re-issue the wait notice.
            try:
                messages = [_text_message(self.pending_text)]
                self._remember_sent_messages(
                    await self._client.reply(reply_token, messages),
                    messages,
                    chat_id=chat_id,
                )
            except Exception:
                pass

    def _prune_group_media_context(self, chat_id: str) -> None:
        ttl = max(0.0, float(self.group_media_context_ttl or 0.0))
        if ttl <= 0 or not chat_id:
            self._group_media_context.pop(chat_id, None)
            return
        cutoff = time.time() - ttl
        entries = [
            entry
            for entry in self._group_media_context.get(chat_id, [])
            if entry[0] >= cutoff
        ][-10:]
        if entries:
            self._group_media_context[chat_id] = entries
        else:
            self._group_media_context.pop(chat_id, None)

    def _remember_group_media(
        self,
        chat_id: str,
        user_id: str,
        local_path: str,
        media_type: str,
    ) -> None:
        if self.group_media_context_ttl <= 0 or not chat_id or not local_path:
            return
        self._prune_group_media_context(chat_id)
        entries = self._group_media_context.setdefault(chat_id, [])
        entries.append((time.time(), user_id or "", local_path, media_type))
        self._group_media_context[chat_id] = entries[-10:]

    def _get_recent_group_media(
        self,
        chat_id: str,
        user_id: str,
    ) -> Optional[Tuple[str, str]]:
        if self.group_media_context_ttl <= 0 or not chat_id:
            return None
        self._prune_group_media_context(chat_id)
        entries = self._group_media_context.get(chat_id, [])
        if not entries:
            return None
        requester_entries = [entry for entry in entries if user_id and entry[1] == user_id]
        entry = (requester_entries or entries)[-1]
        return entry[2], entry[3]

    def _prune_sent_message_ids(self) -> None:
        cutoff = time.time() - SENT_MESSAGE_ID_TTL_SECONDS
        self._sent_message_ids = {
            message_id: ts
            for message_id, ts in self._sent_message_ids.items()
            if ts >= cutoff
        }
        self._sent_message_context = {
            message_id: (ts, text)
            for message_id, (ts, text) in self._sent_message_context.items()
            if ts >= cutoff
        }
        self._sent_message_media_context = {
            message_id: (ts, path, media_type)
            for message_id, (ts, path, media_type) in self._sent_message_media_context.items()
            if ts >= cutoff
        }

    def _remember_sent_messages(
        self,
        sent_messages: Any,
        original_messages: Optional[List[Dict[str, Any]]] = None,
        *,
        chat_id: str = "",
    ) -> None:
        if not isinstance(sent_messages, list):
            return
        if sent_messages:
            self._mark_group_reply(chat_id)
        now = time.time()
        self._prune_sent_message_ids()
        original_messages = original_messages or []
        for idx, item in enumerate(sent_messages):
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("id") or "").strip()
            if message_id:
                self._sent_message_ids[message_id] = now
                if idx < len(original_messages):
                    context = self._summarize_outgoing_message(original_messages[idx])
                    if context:
                        self._sent_message_context[message_id] = (now, context)
                    media_context = self._media_context_for_outgoing_message(original_messages[idx])
                    if media_context:
                        self._sent_message_media_context[message_id] = (
                            now,
                            media_context[0],
                            media_context[1],
                        )
        if len(self._sent_message_ids) > 500:
            newest = sorted(self._sent_message_ids.items(), key=lambda kv: kv[1])[-500:]
            self._sent_message_ids = dict(newest)
            self._sent_message_context = {
                message_id: self._sent_message_context[message_id]
                for message_id in self._sent_message_ids
                if message_id in self._sent_message_context
            }
            self._sent_message_media_context = {
                message_id: self._sent_message_media_context[message_id]
                for message_id in self._sent_message_ids
                if message_id in self._sent_message_media_context
            }

    def _summarize_outgoing_message(self, message: Dict[str, Any]) -> str:
        msg_type = str((message or {}).get("type") or "")
        if msg_type == "text":
            return str(message.get("text") or "")[:1000]
        if msg_type:
            return f"[{msg_type}]"
        return ""

    def _media_context_for_outgoing_message(self, message: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        msg_type = str((message or {}).get("type") or "")
        if msg_type not in {"image", "audio", "video"}:
            return None
        url = str(
            (message or {}).get("originalContentUrl")
            or (message or {}).get("previewImageUrl")
            or ""
        )
        token = self._media_token_from_url(url)
        if not token:
            return None
        entry = self._media_tokens.get(token)
        if not entry:
            return None
        path, expires_at = entry
        if time.time() > expires_at:
            return None
        return path, _line_media_type(msg_type, path)

    def _media_token_from_url(self, url: str) -> str:
        if not url:
            return ""
        match = re.search(r"/line/media/([^/]+)/", url)
        return match.group(1) if match else ""

    def _is_replying_to_sent_message(self, message: Dict[str, Any]) -> bool:
        quoted_message_id = str((message or {}).get("quotedMessageId") or "").strip()
        if not quoted_message_id:
            return False
        self._prune_sent_message_ids()
        return quoted_message_id in self._sent_message_ids

    def _sent_message_text_for_quote(self, message: Dict[str, Any]) -> str:
        quoted_message_id = str((message or {}).get("quotedMessageId") or "").strip()
        if not quoted_message_id:
            return ""
        self._prune_sent_message_ids()
        entry = self._sent_message_context.get(quoted_message_id)
        return entry[1] if entry else ""

    def _sent_message_media_for_quote(self, message: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        quoted_message_id = str((message or {}).get("quotedMessageId") or "").strip()
        if not quoted_message_id:
            return None
        self._prune_sent_message_ids()
        entry = self._sent_message_media_context.get(quoted_message_id)
        if not entry:
            return None
        return entry[1], entry[2]

    def _mark_group_message_seen(self, chat_id: str) -> None:
        if not chat_id:
            return
        now = time.time()
        window = max(0, int(self.group_chime_in_burst_window_seconds or 0))
        cutoff = now - window if window else now
        recent = [
            ts
            for ts in self._group_recent_message_times.get(chat_id, [])
            if ts >= cutoff
        ]
        recent.append(now)
        self._group_recent_message_times[chat_id] = recent[-100:]

    def _recent_group_message_count(self, chat_id: str) -> int:
        window = max(0, int(self.group_chime_in_burst_window_seconds or 0))
        if not chat_id or window <= 0:
            return 0
        cutoff = time.time() - window
        recent = [
            ts
            for ts in self._group_recent_message_times.get(chat_id, [])
            if ts >= cutoff
        ]
        self._group_recent_message_times[chat_id] = recent[-100:]
        return len(recent)

    def _mark_group_reply(self, chat_id: str) -> None:
        if chat_id.startswith(("C", "R")):
            self._group_last_reply_at[chat_id] = time.time()

    def _should_group_chime_in(
        self,
        chat_id: str,
        user_id: str,
        message: Dict[str, Any],
        text: str,
    ) -> Tuple[bool, str]:
        if self.group_reply_mode not in {"mention_keyword_or_chime", "chime_only"}:
            return False, "mode_without_chime"
        if not self.group_chime_in_enabled:
            return False, "chime_disabled"
        if self.group_chime_in_allowed_groups and chat_id not in self.group_chime_in_allowed_groups:
            return False, "group_not_allowed"
        if (message or {}).get("type") != "text":
            return False, "non_text"
        if _message_mentions_self(message):
            return False, "already_mentioned"
        if _find_group_trigger_keyword(text, self.group_trigger_keywords):
            return False, "keyword_trigger"
        if self._is_replying_to_sent_message(message):
            return False, "quoted_sent_message"

        clean_text = (text or "").strip()
        has_quote = bool(str((message or {}).get("quotedMessageId") or "").strip())
        has_recent_media = bool(self._get_recent_group_media(chat_id, user_id))
        if len(clean_text) < max(0, int(self.group_chime_in_min_chars or 0)) and not has_quote and not has_recent_media:
            return False, "too_short"

        now = time.time()
        recent_reply_cooldown = max(
            0,
            int(self.group_chime_in_recent_reply_cooldown_seconds or 0),
        )
        last_reply_at = self._group_last_reply_at.get(chat_id, 0)
        if recent_reply_cooldown and last_reply_at and now - last_reply_at < recent_reply_cooldown:
            return False, "recent_reply"

        state = self._group_chime_state.get(chat_id, {})
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        today_count = int(state.get("today_count") or 0)
        if state.get("last_chime_date") != today:
            today_count = 0
        if today_count >= max(0, int(self.group_chime_in_max_per_day or 0)):
            return False, "daily_limit"

        message_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()[:16]
        if state.get("last_message_hash") == message_hash:
            return False, "same_message"

        score, reasons = _group_chime_score(
            clean_text,
            has_recent_media=has_recent_media,
            has_quote=has_quote,
        )
        min_score = int(self.group_chime_in_min_score or DEFAULT_GROUP_CHIME_IN_MIN_SCORE)
        if score < min_score:
            return False, f"low_score_{score}"

        high_priority_score = int(
            self.group_chime_in_high_priority_score
            or DEFAULT_GROUP_CHIME_IN_HIGH_PRIORITY_SCORE
        )
        cooldown = max(0, int(self.group_chime_in_cooldown_seconds or 0))
        last_chime_at = float(state.get("last_chime_at") or 0)
        if cooldown and last_chime_at and now - last_chime_at < cooldown and score < high_priority_score:
            return False, "chime_cooldown"

        burst_limit = max(0, int(self.group_chime_in_burst_message_count or 0))
        recent_count = self._recent_group_message_count(chat_id)
        if burst_limit and recent_count >= burst_limit and score < high_priority_score:
            return False, f"group_burst_{recent_count}"

        reason = "+".join(reasons[:4]) if reasons else "scored"
        return True, f"chime:{reason}:score={score}"

    def _record_group_chime(self, chat_id: str, text: str, reason: str) -> None:
        if not chat_id:
            return
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        state = self._group_chime_state.get(chat_id, {})
        today_count = int(state.get("today_count") or 0)
        if state.get("last_chime_date") != today:
            today_count = 0
        clean_text = (text or "").strip()
        self._group_chime_state[chat_id] = {
            "last_chime_at": now,
            "last_chime_date": today,
            "today_count": today_count + 1,
            "last_message_hash": hashlib.sha256(clean_text.encode("utf-8")).hexdigest()[:16],
            "last_reason": reason,
        }

    async def _download_media(
        self,
        message_id: str,
        msg_type: str,
        filename: str = "",
        *,
        warn: bool = True,
    ) -> Optional[str]:
        if not self._client or not message_id:
            return None
        try:
            data = await self._client.fetch_content(message_id)
        except Exception as exc:
            log = logger.warning if warn else logger.debug
            log("LINE: failed to fetch %s content for %s: %s", msg_type, message_id, exc)
            return None
        ext = {
            "image": ".jpg",
            "audio": ".m4a",
            "video": ".mp4",
            "file": ".bin",
        }.get(msg_type, ".bin")
        try:
            if msg_type == "image":
                return cache_image_from_bytes(data, ext=ext)
            if msg_type == "audio":
                return cache_audio_from_bytes(data, ext=ext)
            if msg_type == "video":
                return cache_video_from_bytes(data, ext=ext)
            return cache_document_from_bytes(data, filename or f"line_{message_id}{ext}")
        except Exception as exc:
            logger.warning("LINE: failed to cache %s payload: %s", msg_type, exc)
            return None

    # ------------------------------------------------------------------
    # Outbound send (text)
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if _is_internal_notice(content):
            logger.info("LINE: suppressed internal notice for chat %s", chat_id)
            return SendResult(success=True, message_id=None)

        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")

        # System busy-acks (interrupting / queued / steered) bypass the
        # postback cache and route directly to LINE so they reach the user
        # as visible bubbles. Source: PR #18153.
        if _is_system_bypass(content):
            return await self._send_text_chunks(chat_id, content, force_push=False)

        # If the chat has a PENDING postback button outstanding, route the
        # response into the cache for the user to fetch via tap.
        pending_rid = self._pending_buttons.get(chat_id)
        if pending_rid:
            self._cache.set_ready(pending_rid, content)
            return SendResult(success=True, message_id=pending_rid)

        return await self._send_text_chunks(chat_id, content, force_push=False)

    async def _send_text_chunks(
        self,
        chat_id: str,
        content: str,
        *,
        force_push: bool,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")

        chunks = split_for_line(strip_markdown_preserving_urls(content))
        if not chunks:
            return SendResult(success=True, message_id=None)
        messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]

        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply and not force_push:
            try:
                self._remember_sent_messages(
                    await self._client.reply(token, messages),
                    messages,
                    chat_id=chat_id,
                )
                return SendResult(success=True, message_id=token)
            except Exception as exc:
                logger.info("LINE: reply token rejected (%s); falling back to push", exc)
                # fall through to push

        try:
            self._remember_sent_messages(
                await self._client.push(chat_id, messages),
                messages,
                chat_id=chat_id,
            )
            return SendResult(success=True, message_id=None)
        except Exception as exc:
            logger.error("LINE: push send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    def _consume_reply_token(self, chat_id: str) -> Tuple[str, bool]:
        """Consume a stashed reply token if present and unexpired.

        Returns ``(token, used_reply)``.
        """
        entry = self._reply_tokens.pop(chat_id, None)
        if not entry:
            return "", False
        token, expires_at = entry
        if not token or time.time() >= expires_at:
            return "", False
        return token, True

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Trigger LINE's loading-animation indicator (DM only)."""
        if self._client and chat_id:
            await self._client.loading(chat_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Best-effort chat info derived from the chat_id prefix.

        LINE's chat-info APIs are limited and per-source-type — instead of
        chasing them we infer from the well-known ID prefixes:
        ``U`` = user (1:1), ``C`` = group, ``R`` = room. The agent only
        needs ``name`` + ``type`` from this method.
        """
        prefix = (chat_id or "")[:1]
        chat_type = {"U": "dm", "C": "group", "R": "channel"}.get(prefix, "dm")
        return {"name": chat_id or "", "type": chat_type}

    def format_message(self, content: str) -> str:
        """Strip Markdown that LINE can't render. URLs are preserved."""
        return strip_markdown_preserving_urls(content)

    # ------------------------------------------------------------------
    # Slow-LLM postback button — driven by _keep_typing
    # ------------------------------------------------------------------

    async def _keep_typing(self, chat_id: str, *args, **kwargs) -> None:
        """Override the base loop to fire the postback button at threshold.

        We intentionally keep the base implementation behind us: it's
        responsible for the typing-indicator heartbeat, while *this*
        wrapper layers in the slow-LLM postback bubble at threshold.
        """
        if (
            self.slow_response_threshold <= 0
            or not self._client
            or not chat_id
        ):
            await super()._keep_typing(chat_id, *args, **kwargs)
            return

        async def _fire_postback() -> None:
            try:
                await asyncio.sleep(self.slow_response_threshold)
            except asyncio.CancelledError:
                raise
            # Only fire if we still have a usable reply token. If the agent
            # already responded, _consume_reply_token has cleared it.
            if chat_id not in self._reply_tokens:
                return
            if chat_id in self._pending_buttons:
                return
            rid = self._cache.register_pending(chat_id)
            self._pending_buttons[chat_id] = rid
            token, used = self._consume_reply_token(chat_id)
            if not used:
                self._pending_buttons.pop(chat_id, None)
                return
            msg = build_postback_button_message(
                self.pending_text, self.button_label, rid
            )
            try:
                self._remember_sent_messages(
                    await self._client.reply(token, [msg]),
                    [msg],
                    chat_id=chat_id,
                )
                logger.info("LINE: sent slow-LLM postback button for chat %s (rid=%s)", chat_id, rid)
            except Exception as exc:
                logger.warning("LINE: postback button send failed: %s", exc)
                self._pending_buttons.pop(chat_id, None)

        post_task = asyncio.create_task(_fire_postback())
        try:
            await super()._keep_typing(chat_id, *args, **kwargs)
        finally:
            if not post_task.done():
                post_task.cancel()
                try:
                    await post_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
        """Resolve any orphan PENDING postback so the button doesn't loop."""
        await super().interrupt_session_activity(session_key, chat_id)
        rid = self._pending_buttons.pop(chat_id, None)
        if rid:
            self._cache.set_error(rid, self.interrupted_text)

    # ------------------------------------------------------------------
    # Outbound media (image / voice / video)
    # ------------------------------------------------------------------

    def _register_media(self, file_path: str, *, cleanup: bool = False) -> str:
        """Register a local file for HTTPS serving; return the URL token."""
        # Evict expired tokens first.
        now = time.time()
        for token in list(self._media_tokens.keys()):
            path, exp = self._media_tokens[token]
            if now > exp:
                self._media_tokens.pop(token, None)
                if path in self._media_temp_paths:
                    self._media_temp_paths.discard(path)
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        resolved = str(Path(file_path).resolve())
        token = secrets.token_urlsafe(32)
        self._media_tokens[token] = (resolved, now + self._media_ttl)
        if cleanup:
            self._media_temp_paths.add(resolved)
        return token

    def _media_url(self, token: str, filename: str) -> str:
        """Build the public HTTPS URL for a media token. PR #8398 style."""
        if self.public_base_url:
            base = self.public_base_url
        else:
            host = self.webhook_host
            port = self.webhook_port
            if port == 443:
                base = f"https://{host}"
            else:
                base = f"https://{host}:{port}"
        safe_name = _urlquote(filename, safe="")
        return f"{base}{DEFAULT_MEDIA_PATH_PREFIX}/{token}/{safe_name}"

    async def _handle_media(self, request) -> Any:
        """Serve a registered local file over HTTPS for LINE's media URLs.

        Defence-in-depth: even though ``_register_media`` is only called
        from trusted internal code, we recheck the resolved path against
        an allowed-roots set before serving. Sources allowed:
        ``tempfile.gettempdir()``, ``/tmp`` (which resolves to
        ``/private/tmp`` on macOS), and ``HERMES_HOME``. PR #8398.
        """
        from aiohttp import web

        token = request.match_info["token"]
        entry = self._media_tokens.get(token)
        if not entry:
            return web.Response(status=404, text="not found")

        file_path, expires_at = entry
        if time.time() > expires_at:
            self._media_tokens.pop(token, None)
            return web.Response(status=410, text="gone")

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return web.Response(status=404, text="not found")

        try:
            from hermes_constants import get_hermes_home
            hermes_home = Path(get_hermes_home()).resolve()
        except Exception:
            hermes_home = Path.home().joinpath(".hermes").resolve()

        allowed_roots = {
            Path(tempfile.gettempdir()).resolve(),
            Path("/tmp").resolve(),  # → /private/tmp on macOS
            hermes_home,
        }
        resolved = path.resolve()
        if not any(_is_relative_to(resolved, r) for r in allowed_roots):
            logger.warning("LINE: refusing to serve outside allowed roots: %s", resolved)
            return web.Response(status=403, text="forbidden")

        content_type, _ = mimetypes.guess_type(str(path))
        return web.FileResponse(
            path,
            headers={"Content-Type": content_type or "application/octet-stream"},
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"image file not found: {image_path}")
        if path.stat().st_size > LINE_IMAGE_MAX_BYTES:
            return SendResult(success=False, error="image exceeds 10 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not self.public_base_url and self.webhook_host == "0.0.0.0":
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send images "
                "(LINE only accepts publicly reachable HTTPS URLs)",
            )

        token = self._register_media(str(path.resolve()))
        url = self._media_url(token, path.name)
        if not url.lower().startswith("https://"):
            return SendResult(success=False, error=f"LINE image URL must be HTTPS: {url}")
        msgs: List[Dict[str, Any]] = [_image_message(url)]
        if caption:
            msgs.append(_text_message(caption))
        return await self._send_messages(chat_id, msgs)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        duration_ms: int = 1000,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = Path(audio_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"audio file not found: {audio_path}")
        if path.stat().st_size > LINE_AV_MAX_BYTES:
            return SendResult(success=False, error="audio exceeds 200 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not self.public_base_url and self.webhook_host == "0.0.0.0":
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send audio",
            )

        token = self._register_media(str(path.resolve()))
        url = self._media_url(token, path.name)
        return await self._send_messages(chat_id, [_audio_message(url, duration_ms)])

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        preview_path: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        path = Path(video_path)
        if not path.exists() or not path.is_file():
            return SendResult(success=False, error=f"video file not found: {video_path}")
        if path.stat().st_size > LINE_AV_MAX_BYTES:
            return SendResult(success=False, error="video exceeds 200 MB LINE limit")
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not self.public_base_url and self.webhook_host == "0.0.0.0":
            return SendResult(
                success=False,
                error="LINE_PUBLIC_URL must be set to send video",
            )

        # LINE requires a previewImageUrl. Use one if supplied, otherwise
        # write a stdlib 1×1 PNG to /tmp and serve it. PR #8398.
        if preview_path and Path(preview_path).is_file():
            preview_token = self._register_media(str(Path(preview_path).resolve()))
            preview_filename = Path(preview_path).name
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            try:
                tmp.write(_FALLBACK_PNG_PREVIEW)
                tmp.flush()
                tmp.close()
                preview_token = self._register_media(tmp.name, cleanup=True)
                preview_filename = "preview.png"
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise

        video_token = self._register_media(str(path.resolve()))
        video_url = self._media_url(video_token, path.name)
        preview_url = self._media_url(preview_token, preview_filename)
        return await self._send_messages(chat_id, [_video_message(video_url, preview_url)])

    async def _send_messages(
        self,
        chat_id: str,
        messages: List[Dict[str, Any]],
    ) -> SendResult:
        """Send already-built message objects, batched at 5/call."""
        if not self._client:
            return SendResult(success=False, error="LINE adapter not connected")
        if not messages:
            return SendResult(success=True, message_id=None)

        first_batch = messages[:LINE_MAX_MESSAGES_PER_CALL]
        rest = messages[LINE_MAX_MESSAGES_PER_CALL:]

        # First batch: try reply token, fall back to push.
        token, used_reply = self._consume_reply_token(chat_id)
        if used_reply:
            try:
                self._remember_sent_messages(
                    await self._client.reply(token, first_batch),
                    first_batch,
                    chat_id=chat_id,
                )
            except Exception as exc:
                logger.info("LINE: reply token rejected (%s); falling back to push", exc)
                try:
                    self._remember_sent_messages(
                        await self._client.push(chat_id, first_batch),
                        first_batch,
                        chat_id=chat_id,
                    )
                except Exception as exc2:
                    return SendResult(success=False, error=str(exc2))
        else:
            try:
                self._remember_sent_messages(
                    await self._client.push(chat_id, first_batch),
                    first_batch,
                    chat_id=chat_id,
                )
            except Exception as exc:
                return SendResult(success=False, error=str(exc))

        # Subsequent batches: always push (reply token is single-use).
        while rest:
            batch = rest[:LINE_MAX_MESSAGES_PER_CALL]
            rest = rest[LINE_MAX_MESSAGES_PER_CALL:]
            try:
                self._remember_sent_messages(
                    await self._client.push(chat_id, batch),
                    batch,
                    chat_id=chat_id,
                )
            except Exception as exc:
                logger.warning("LINE: push for follow-up batch failed: %s", exc)
                return SendResult(success=False, error=str(exc))

        return SendResult(success=True, message_id=None)


def _is_relative_to(child: Path, parent: Path) -> bool:
    """Backport for Path.is_relative_to (Python 3.9+) — defensive against
    cwd-resolution differences across CI runners."""
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (AttributeError, ValueError):
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp at runtime."""
    if not os.getenv("LINE_CHANNEL_ACCESS_TOKEN"):
        return False
    if not os.getenv("LINE_CHANNEL_SECRET"):
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    has_token = bool(
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or extra.get("channel_access_token")
    )
    has_secret = bool(
        os.getenv("LINE_CHANNEL_SECRET") or extra.get("channel_secret")
    )
    return has_token and has_secret


def is_connected(config) -> bool:
    """Surface in ``hermes status`` even before the adapter is instantiated."""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Auto-seed PlatformConfig.extra from env-only setups.

    Lets ``hermes status`` reflect a LINE configuration that lives entirely
    in ``.env`` without a ``platforms.line`` block in ``config.yaml``.
    Mirrors the IRC plugin's pattern.
    """
    if not (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") and os.getenv("LINE_CHANNEL_SECRET")):
        return None
    seeded: Dict[str, Any] = {}
    if os.getenv("LINE_PORT"):
        try:
            seeded["port"] = int(os.environ["LINE_PORT"])
        except ValueError:
            pass
    if os.getenv("LINE_HOST"):
        seeded["host"] = os.environ["LINE_HOST"]
    if os.getenv("LINE_PUBLIC_URL"):
        seeded["public_url"] = os.environ["LINE_PUBLIC_URL"]
    if os.getenv("LINE_HOME_CHANNEL"):
        seeded["home_channel"] = os.environ["LINE_HOME_CHANNEL"]
    return seeded or {}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process push delivery for cron jobs running detached from the gateway.

    Without this hook ``deliver=line`` cron jobs fail with ``no live adapter``
    when cron runs as its own process. We always Push (reply tokens require
    an inbound webhook event we don't have in this path).

    ``thread_id`` is accepted for signature parity but ignored — LINE has
    no native thread primitive on the channel-side API. ``media_files``
    likewise: cron-side media delivery requires a publicly-reachable URL,
    which the standalone path can't construct without binding the webhook
    server, so we send a text reference instead.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    token = (
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or extra.get("channel_access_token", "")
    )
    if not token or not chat_id:
        return {"error": "LINE standalone send: missing token or chat_id"}

    plain = strip_markdown_preserving_urls(message or "")
    chunks = split_for_line(plain) or [""]
    messages = [_text_message(c) for c in chunks][:LINE_MAX_MESSAGES_PER_CALL]
    if media_files:
        # Tack on a hint so the recipient knows media was generated but not delivered.
        messages.append(_text_message(f"[{len(media_files)} attachment(s) generated; not deliverable from cron]"))
        messages = messages[:LINE_MAX_MESSAGES_PER_CALL]

    client = _LineClient(token)
    try:
        await client.push(chat_id, messages)
        return {"success": True, "message_id": None}
    except Exception as exc:
        return {"error": str(exc)}


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup line``.

    Mirrors the irc/teams style: prompts for the two required vars, plus
    one optional public URL. Writes to ``~/.hermes/.env`` via ``hermes_cli.config``.
    """
    print()
    print("LINE Messaging API setup")
    print("------------------------")
    print("Create a Messaging API channel at https://developers.line.biz/console/")
    print("then copy the values below.")
    print()

    try:
        from hermes_cli.config import get_env_var, set_env_var
    except ImportError:
        print("hermes_cli.config not available; set LINE_* vars manually in ~/.hermes/.env")
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = get_env_var(var) if callable(get_env_var) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                import getpass
                value = getpass.getpass(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            set_env_var(var, value)

    _prompt("LINE_CHANNEL_ACCESS_TOKEN", "Channel access token", secret=True)
    _prompt("LINE_CHANNEL_SECRET", "Channel secret", secret=True)
    _prompt("LINE_PUBLIC_URL", "Public HTTPS base URL (optional, e.g. https://my-tunnel.example.com)")
    _prompt("LINE_ALLOWED_USERS", "Allowed user IDs (comma-separated; blank=skip)")
    print("Done. Set the webhook URL in the LINE console to "
          "<your-public-url>/line/webhook and enable 'Use webhook'.")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="line",
        label="LINE",
        adapter_factory=lambda cfg: LineAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"],
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="LINE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="LINE_ALLOWED_USERS",
        allow_all_env="LINE_ALLOW_ALL_USERS",
        # LINE per-bubble cap is 5000; smart-chunker uses 4500.
        max_message_length=LINE_SAFE_BUBBLE_CHARS,
        emoji="💚",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via LINE Messaging API. LINE does NOT render "
            "Markdown — text bubbles show ** and # literally. Bare URLs are "
            "auto-linked, but \\[label\\](url) syntax is not. Each text bubble "
            "is capped at 5000 characters and at most 5 bubbles are sent per "
            "reply, so keep responses concise. Image/audio/video sending "
            "requires LINE_PUBLIC_URL configured to a publicly reachable HTTPS "
            "host. Slow responses surface a 'Get answer' button the user taps "
            "to fetch the reply via a fresh free token."
        ),
    )
