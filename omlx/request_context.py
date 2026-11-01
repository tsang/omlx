# SPDX-License-Identifier: Apache-2.0
"""Process-global registry of display metadata for in-flight inference requests.

The scheduler and engines track token counts per request id; they know
nothing about who is talking (client IP, user-agent, which API surface) or
what they asked (a short preview of the last message, whether the payload
carried images/audio/files). This registry keys that display-only context by
the engine request id — the same id surfaced by the ``X-oMLX-Request-ID``
response header and listed on the admin ``/api/stats`` request rows.

Routes register context right after minting the inference request id (the
request path never blocks on it: failures are swallowed). ``/api/stats``
looks entries up when composing waiting/generating/prefill rows. Requests
end without a guaranteed hook here (clients disconnect, engines abort), so
entries self-expire after a TTL and the registry is LRU-capped; churn from a
busy server never grows memory. Previews are kept in RAM only and the stats
endpoint is admin-gated, matching the trust level of the api key it already
returns.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# Max characters kept in the last-message preview shown on the dashboard.
PREVIEW_CHARS = 140
# Max characters kept from the client User-Agent header.
_UA_CHARS = 120
# Registry bounds. TTL comfortably exceeds the longest plausible request
# (a prefill of a giant context on a congested queue); stale entries are
# invisible to lookups once the request leaves the scheduler anyway.
_MAX_ENTRIES = 4096
_TTL_S = 12 * 3600.0
# Preview scanning never touches more of a huge payload than this.
_SCAN_CAP = 4096

_WHITESPACE_RE = re.compile(r"[\s\u0000-\u001f]+")

# Content-part type -> media category counted for the row badge.
_MEDIA_KINDS = {
    "image_url": "img",
    "image": "img",
    "input_image": "img",
    "input_audio": "audio",
    "audio": "audio",
    "audio_url": "audio",
    "video_url": "video",
    "input_video": "video",
    "file": "file",
    "document": "doc",
}
_TEXT_KEYS = ("text", "input_text", "output_text", "thinking")


def _clean(text: str, cap: int = _SCAN_CAP) -> str:
    """Collapse whitespace/control chars and bound the scanned span."""
    if len(text) > cap:
        # Keep the tail: for prompts the continuation point matters most,
        # and the final clamp applies the head/tail policy later.
        text = text[-cap:]
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _clamp_head(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "..."


def _clamp_tail(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return "..." + text[-n:].lstrip()


def _content_text(content: Any, media: dict[str, int], _depth: int = 0) -> str:
    """Extract preview text from a message content value, counting media.

    Handles OpenAI chat parts (text/image_url/input_audio/video_url/file),
    Anthropic blocks (text/image/document/tool_use/tool_result/thinking), and
    plain strings. data: URLs are never copied into the preview.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return _clean(content)
    if _depth > 2:
        return ""
    pieces: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                pieces.append(_clean(part))
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in _MEDIA_KINDS:
                media[_MEDIA_KINDS[ptype]] = media.get(_MEDIA_KINDS[ptype], 0) + 1
            elif ptype in _TEXT_KEYS:
                value = part.get(ptype)
                if isinstance(value, str):
                    pieces.append(_clean(value))
            elif ptype == "tool_use":
                name = part.get("name")
                if isinstance(name, str) and name:
                    pieces.append(f"[tool:{_clean(name, 64)}]")
            elif ptype == "tool_result":
                inner = _content_text(part.get("content"), media, _depth + 1)
                if inner:
                    pieces.append("[result:" + inner + "]")
    return " ".join(p for p in pieces if p).strip()


def _media_label(media: dict[str, int]) -> str | None:
    if not media:
        return None
    order = ("img", "audio", "video", "file", "doc")
    return " · ".join(f"{k}×{media[k]}" for k in order if media.get(k))


def preview_messages(messages: Any) -> tuple[str | None, str | None]:
    """(preview, media_label) for the most informative recent message."""
    if not isinstance(messages, list):
        return None, None
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "?"
        media: dict[str, int] = {}
        text = _content_text(msg.get("content"), media)
        if not text:
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                names = []
                for call in tool_calls[:4]:
                    if isinstance(call, dict):
                        fn = call.get("function")
                        name = fn.get("name") if isinstance(fn, dict) else None
                        names.append(_clean(name, 48) if isinstance(name, str) else "?")
                if names:
                    text = "tool_calls: " + ", ".join(names)
        label = _media_label(media)
        if text:
            return _clamp_head(f"{role}: {text}", PREVIEW_CHARS), label
        if label:
            return f"{role}: [media]", label
    return None, None


def preview_prompt(prompt: Any) -> str | None:
    """Tail preview for OpenAI completions prompts (str or list[str])."""
    if isinstance(prompt, list):
        prompt = prompt[-1] if prompt else None
    if not isinstance(prompt, str) or not prompt:
        return None
    return _clamp_tail(_clean(prompt), PREVIEW_CHARS)


def client_ip(http_request: Any) -> str | None:
    """Best-effort client address, preferring reverse-proxy headers.

    X-Forwarded-For/X-Real-IP are honored as *display* hints only (first
    hop wins); the socket peer is the fallback. This is dashboard context,
    not an auth decision.
    """
    try:
        headers = http_request.headers
        xff = headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first[:64]
        real = headers.get("x-real-ip")
        if real and real.strip():
            return real.strip()[:64]
        client = getattr(http_request, "client", None)
        host = getattr(client, "host", None)
        return host[:64] if host else None
    except Exception:  # noqa: BLE001
        return None


class RequestContextRegistry:
    """Thread-safe LRU+TTL store of per-request display metadata."""

    def __init__(self, max_entries: int = _MAX_ENTRIES, ttl_s: float = _TTL_S):
        self._max_entries = max(1, max_entries)
        self._ttl_s = ttl_s
        self._entries: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def register(self, request_id: str, **fields: Any) -> None:
        if not request_id:
            return
        entry = {k: v for k, v in fields.items() if v}
        now = time.monotonic()
        with self._lock:
            self._entries[request_id] = (now, entry)
            self._entries.move_to_end(request_id)
            cutoff = now - self._ttl_s
            while self._entries and self._entries[next(iter(self._entries))][0] < cutoff:
                self._entries.popitem(last=False)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def get(self, request_id: Any) -> dict | None:
        if not request_id:
            return None
        now = time.monotonic()
        with self._lock:
            found = self._entries.get(request_id)
            if found is None:
                return None
            ts, entry = found
            if now - ts >= self._ttl_s:
                self._entries.pop(request_id, None)
                return None
            return dict(entry)

    def remove(self, request_id: Any) -> None:
        if not request_id:
            return
        with self._lock:
            self._entries.pop(request_id, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_registry: RequestContextRegistry | None = None
_registry_lock = threading.Lock()


def get_request_context_registry() -> RequestContextRegistry:
    """Get or create the process-global RequestContextRegistry."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = RequestContextRegistry()
    return _registry


def register_inference_context(
    request_id: str,
    http_request: Any,
    *,
    source: str,
    messages: Any = None,
    prompt: Any = None,
) -> None:
    """Attach client/preview display metadata to a minted inference id.

    Never raises: dashboard context is opportunistic and must not affect
    the serving path.
    """
    try:
        preview = media = None
        if messages is not None:
            preview, media = preview_messages(messages)
        elif prompt is not None:
            preview = preview_prompt(prompt)
        ua = None
        raw_ua = http_request.headers.get("user-agent")
        if raw_ua:
            ua = _clamp_head(_clean(raw_ua, _UA_CHARS), _UA_CHARS) or None
        referer = None
        raw_ref = http_request.headers.get("referer") or http_request.headers.get(
            "referrer"
        )
        if raw_ref:
            referer = _clamp_head(_clean(raw_ref, _UA_CHARS), _UA_CHARS) or None
        get_request_context_registry().register(
            request_id,
            source=source,
            client_ip=client_ip(http_request),
            user_agent=ua,
            referer=referer,
            preview=preview,
            media=media,
        )
    except Exception:  # noqa: BLE001
        logger.debug("request context registration failed", exc_info=True)
