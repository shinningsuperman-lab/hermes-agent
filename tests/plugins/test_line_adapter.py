from types import SimpleNamespace

import pytest

from gateway.platforms.base import MessageType
from plugins.platforms.line import adapter as line_adapter


def test_line_image_message_type_normalizes_to_photo():
    assert line_adapter._line_message_type("image") is MessageType.PHOTO
    assert line_adapter._line_media_type("image") == "image/jpeg"


@pytest.mark.asyncio
async def test_line_image_message_event_uses_photo_and_image_mime(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    captured = []

    async def fake_download_media(message_id, msg_type, filename=""):
        assert message_id == "line-image-1"
        assert msg_type == "image"
        return "/tmp/line-image-1.jpg"

    async def fake_handle_message(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_download_media", fake_download_media)
    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    await adapter._handle_message_event(
        {
            "replyToken": "reply-token",
            "source": {"type": "user", "userId": "U-test"},
            "message": {"type": "image", "id": "line-image-1"},
        }
    )

    assert len(captured) == 1
    event = captured[0]
    assert event.text == "[image]"
    assert event.message_type is MessageType.PHOTO
    assert event.media_urls == ["/tmp/line-image-1.jpg"]
    assert event.media_types == ["image/jpeg"]


@pytest.mark.asyncio
async def test_line_group_trigger_filters_and_strips_text(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.group_trigger_keywords = ["@daisy", "黛西"]
    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    await adapter._handle_message_event(
        {
            "replyToken": "reply-token-1",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {"type": "text", "id": "line-text-1", "text": "普通聊天"},
        }
    )
    await adapter._handle_message_event(
        {
            "replyToken": "reply-token-2",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {"type": "text", "id": "line-text-2", "text": "@daisy：測試"},
        }
    )

    assert len(captured) == 1
    assert captured[0].text == "測試"
    assert captured[0].source.chat_type == "group"


@pytest.mark.asyncio
async def test_line_group_text_reply_attaches_quoted_image(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.group_trigger_keywords = ["@daisy"]
    captured = []

    async def fake_download_media(message_id, msg_type, filename="", *, warn=True):
        assert message_id == "quoted-image-1"
        assert msg_type == "image"
        return "/tmp/quoted-image-1.jpg"

    async def fake_handle_message(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_download_media", fake_download_media)
    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    await adapter._handle_message_event(
        {
            "replyToken": "reply-token",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {
                "type": "text",
                "id": "line-text-1",
                "text": "@daisy 看一下這個成績",
                "quotedMessageId": "quoted-image-1",
            },
        }
    )

    assert len(captured) == 1
    assert captured[0].text == "看一下這個成績"
    assert captured[0].message_type is MessageType.PHOTO
    assert captured[0].media_urls == ["/tmp/quoted-image-1.jpg"]
    assert captured[0].media_types == ["image/jpeg"]


@pytest.mark.asyncio
async def test_line_group_image_context_is_cached_then_attached(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.group_trigger_keywords = ["@daisy"]
    adapter.group_media_context_ttl = 600
    captured = []

    async def fake_download_media(message_id, msg_type, filename="", *, warn=True):
        assert message_id == "line-image-1"
        assert msg_type == "image"
        return "/tmp/line-image-1.jpg"

    async def fake_handle_message(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_download_media", fake_download_media)
    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    await adapter._handle_message_event(
        {
            "replyToken": "reply-token-1",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {"type": "image", "id": "line-image-1"},
        }
    )
    await adapter._handle_message_event(
        {
            "replyToken": "reply-token-2",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {"type": "text", "id": "line-text-1", "text": "@daisy 看這張"},
        }
    )

    assert len(captured) == 1
    assert captured[0].text == "看這張"
    assert captured[0].message_type is MessageType.PHOTO
    assert captured[0].media_urls == ["/tmp/line-image-1.jpg"]
    assert captured[0].media_types == ["image/jpeg"]
