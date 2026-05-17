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
