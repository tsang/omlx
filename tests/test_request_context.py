# SPDX-License-Identifier: Apache-2.0
"""Tests for the in-flight request display-context registry."""

import time

from omlx.request_context import (
    PREVIEW_CHARS,
    RequestContextRegistry,
    client_ip,
    get_request_context_registry,
    preview_messages,
    preview_prompt,
    register_inference_context,
)


class _FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _FakeRequest:
    def __init__(self, headers=None, peer=None):
        self.headers = _FakeHeaders(headers or {})
        self.client = peer


def test_preview_uses_last_message_of_any_role():
    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "follow up about the weather"},
    ]
    preview, media = preview_messages(messages)
    assert preview == "user: follow up about the weather"
    assert media is None


def test_preview_counts_media_and_never_leaks_data_urls():
    data_url = "data:image/png;base64," + "A" * 4096
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "compare these"},
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                {"type": "input_audio", "input_audio": {"format": "wav"}},
            ],
        }
    ]
    preview, media = preview_messages(messages)
    assert preview == "user: compare these"
    assert "img×2" in media and "audio×1" in media
    assert "base64" not in preview and "AAAA" not in preview


def test_preview_media_only_message_has_marker():
    messages = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}
    ]
    preview, media = preview_messages(messages)
    assert preview == "user: [media]"
    assert media == "img×1"


def test_preview_tool_calls_and_tool_results():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": "search_web"}}],
        }
    ]
    preview, _ = preview_messages(messages)
    assert preview == "assistant: tool_calls: search_web"

    messages = [
        {
            "role": "tool",
            "content": [
                {"type": "tool_result", "content": [{"type": "text", "text": "found 3"}]}
            ],
        }
    ]
    preview, _ = preview_messages(messages)
    assert preview == "tool: [result:found 3]"


def test_preview_collapses_whitespace_and_truncates():
    long_text = "line one\n\t" + "x" * 400 + "\nline two"
    preview, _ = preview_messages([{"role": "user", "content": long_text}])
    assert "\n" not in preview and "\t" not in preview
    assert preview.startswith("user: ") and preview.endswith("...")
    assert len(preview) <= len("user: ") + PREVIEW_CHARS + len("...")


def test_prompt_preview_is_tail_of_last_prompt():
    preview = preview_prompt(["short", "b" * 500 + "ENDING"])
    assert preview.endswith("ENDING")
    assert preview.startswith("...")
    assert len(preview) <= PREVIEW_CHARS + 3
    assert preview_prompt([]) is None
    assert preview_prompt("") is None


def test_client_ip_prefers_forwarded_headers_then_peer():
    req = _FakeRequest({"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    assert client_ip(req) == "203.0.113.7"

    req = _FakeRequest({"x-real-ip": "198.51.100.4"})
    assert client_ip(req) == "198.51.100.4"

    peer = type("Addr", (), {"host": "127.0.0.1"})()
    assert client_ip(_FakeRequest({}, peer)) == "127.0.0.1"
    assert client_ip(_FakeRequest()) is None


def test_registry_ttl_expiry_and_lru_cap():
    reg = RequestContextRegistry(max_entries=2, ttl_s=0.05)
    reg.register("a", client_ip="1.1.1.1")
    reg.register("b", preview="p")
    assert len(reg) == 2
    reg.register("c")
    assert len(reg) == 2
    assert reg.get("a") is None  # evicted by cap
    assert reg.get("c") == {}  # falsy fields dropped at register
    time.sleep(0.06)
    assert reg.get("c") is None  # ttl expired


def test_register_inference_context_swallow_failures_and_registers(monkeypatch):
    class _NoHeaders:
        @property
        def headers(self):
            raise RuntimeError("socket closed")

    registry = RequestContextRegistry()
    monkeypatch.setattr(
        "omlx.request_context.get_request_context_registry", lambda: registry
    )

    # A broken request object must not raise on the serving path.
    register_inference_context("rid-x", _NoHeaders(), source="openai/chat")
    assert registry.get("rid-x") is None

    peer = type("Addr", (), {"host": "192.168.1.50"})()
    req = _FakeRequest(
        {
            "user-agent": "python-requests/2.32.3",
            "referer": "https://example.com/chat",
        },
        peer,
    )
    register_inference_context(
        "rid-y",
        req,
        source="openai/chat",
        messages=[{"role": "user", "content": "hello server"}],
    )
    ctx = registry.get("rid-y")
    assert ctx["source"] == "openai/chat"
    assert ctx["client_ip"] == "192.168.1.50"
    assert ctx["preview"] == "user: hello server"
    assert ctx["user_agent"].startswith("python-requests")
    assert ctx["referer"] == "https://example.com/chat"


def test_global_registry_singleton():
    assert get_request_context_registry() is get_request_context_registry()
