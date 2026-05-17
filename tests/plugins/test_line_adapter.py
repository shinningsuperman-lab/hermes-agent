from pathlib import Path
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
    adapter.group_reply_mode = "mention_or_keyword"
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
async def test_line_group_native_mention_triggers_without_keyword(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.group_trigger_keywords = ["@daisy"]
    adapter.group_reply_mode = "mention_or_keyword"
    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    await adapter._handle_message_event(
        {
            "replyToken": "reply-token",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {
                "type": "text",
                "id": "line-text-1",
                "text": "看一下這個",
                "mention": {"mentionees": [{"isSelf": True}]},
            },
        }
    )

    assert len(captured) == 1
    assert captured[0].text == "看一下這個"


@pytest.mark.asyncio
async def test_line_group_reply_to_sent_message_triggers_without_keyword(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.group_trigger_keywords = ["@daisy"]
    adapter.group_reply_mode = "mention_or_keyword"
    adapter._remember_sent_messages(
        [{"id": "daisy-message-1"}],
        [{"type": "text", "text": "這是 Daisy 前一則回覆"}],
    )
    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    await adapter._handle_message_event(
        {
            "replyToken": "reply-token",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {
                "type": "text",
                "id": "line-text-1",
                "text": "這個是什麼意思",
                "quotedMessageId": "daisy-message-1",
            },
        }
    )

    assert len(captured) == 1
    assert captured[0].text == "這個是什麼意思"
    assert captured[0].reply_to_message_id == "daisy-message-1"
    assert captured[0].reply_to_text == "這是 Daisy 前一則回覆"


@pytest.mark.asyncio
async def test_line_group_reply_to_sent_image_attaches_original_media(monkeypatch, tmp_path):
    image_path = tmp_path / "daisy-sticker.jpg"
    image_path.write_bytes(b"fake-jpeg")

    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.public_base_url = "https://line.example.com"
    adapter.group_trigger_keywords = ["@daisy"]
    adapter.group_reply_mode = "mention_or_keyword"
    token = adapter._register_media(str(image_path))
    image_url = adapter._media_url(token, image_path.name)
    adapter._remember_sent_messages(
        [{"id": "daisy-image-1"}],
        [{"type": "image", "originalContentUrl": image_url, "previewImageUrl": image_url}],
    )
    captured = []

    async def fake_handle_message(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle_message)

    await adapter._handle_message_event(
        {
            "replyToken": "reply-token",
            "source": {"type": "group", "groupId": "C-test", "userId": "U-test"},
            "message": {
                "type": "text",
                "id": "line-text-1",
                "text": "把它存起來以後用",
                "quotedMessageId": "daisy-image-1",
            },
        }
    )

    assert len(captured) == 1
    assert captured[0].text == "把它存起來以後用"
    assert captured[0].message_type is MessageType.PHOTO
    assert captured[0].media_urls == [str(image_path.resolve())]
    assert captured[0].media_types == ["image/jpeg"]


@pytest.mark.asyncio
async def test_line_group_reply_to_recent_sent_image_falls_back_by_chat(monkeypatch, tmp_path):
    image_path = tmp_path / "daisy-sticker.jpg"
    image_path.write_bytes(b"fake-jpeg")

    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.public_base_url = "https://line.example.com"
    adapter.group_trigger_keywords = ["@daisy"]
    adapter.group_reply_mode = "mention_or_keyword"
    token = adapter._register_media(str(image_path))
    image_url = adapter._media_url(token, image_path.name)
    adapter._remember_sent_messages(
        [],
        [{"type": "image", "originalContentUrl": image_url, "previewImageUrl": image_url}],
        chat_id="C-test",
    )
    captured = []

    async def fake_download_media(message_id, msg_type, filename="", *, warn=True):
        return None

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
                "text": "把它存起來以後用",
                "quotedMessageId": "unknown-line-image-id",
            },
        }
    )

    assert len(captured) == 1
    assert captured[0].message_type is MessageType.PHOTO
    assert captured[0].media_urls == [str(image_path.resolve())]
    assert captured[0].media_types == ["image/jpeg"]


@pytest.mark.asyncio
async def test_line_send_image_file_stages_unservable_path(monkeypatch, tmp_path):
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    image_path = external_dir / "reaction.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    allowed_dir = tmp_path / "allowed"
    cache_dir = tmp_path / "home" / "cache" / "line-media"
    allowed_dir.mkdir()

    class FakeLineClient:
        def __init__(self):
            self.messages = None

        async def push(self, chat_id, messages):
            self.messages = messages
            return [{"id": "sent-image-1"}]

    fake_client = FakeLineClient()
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter._client = fake_client
    adapter.public_base_url = "https://line.example.com"

    monkeypatch.setattr(
        line_adapter,
        "_line_media_allowed_roots",
        lambda: {allowed_dir.resolve()},
    )
    monkeypatch.setattr(line_adapter, "_line_media_cache_dir", lambda: cache_dir)

    result = await adapter.send_image_file("U-test", str(image_path))

    assert result.success
    assert fake_client.messages
    image_url = fake_client.messages[0]["originalContentUrl"]
    token = adapter._media_token_from_url(image_url)
    staged_path, _ = adapter._media_tokens[token]
    assert str(cache_dir.resolve()) in staged_path
    assert image_path.read_bytes() == Path(staged_path).read_bytes()


@pytest.mark.asyncio
async def test_line_group_text_reply_attaches_quoted_image(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.group_trigger_keywords = ["@daisy"]
    adapter.group_reply_mode = "mention_or_keyword"
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
    adapter.group_reply_mode = "mention_or_keyword"
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


@pytest.mark.asyncio
async def test_line_group_chime_handles_implicit_image_request(monkeypatch):
    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter.group_trigger_keywords = ["@daisy"]
    adapter.group_reply_mode = "mention_keyword_or_chime"
    adapter.group_chime_in_enabled = True
    adapter.group_chime_in_cooldown_seconds = 0
    adapter.group_chime_in_min_score = 3
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
            "message": {"type": "text", "id": "line-text-1", "text": "幫我看一下這張成績"},
        }
    )

    assert len(captured) == 1
    assert captured[0].text == "幫我看一下這張成績"
    assert captured[0].message_type is MessageType.PHOTO
    assert captured[0].media_urls == ["/tmp/line-image-1.jpg"]


@pytest.mark.asyncio
async def test_line_postback_ready_delivers_cached_media(monkeypatch, tmp_path):
    image_path = tmp_path / "xiao-si.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    cache_dir = tmp_path / "line-media"

    class FakeLineClient:
        def __init__(self):
            self.replies = []
            self.pushes = []

        async def reply(self, reply_token, messages):
            self.replies.append((reply_token, messages))
            return [{"id": f"reply-{idx}"} for idx, _ in enumerate(messages)]

        async def push(self, chat_id, messages):
            self.pushes.append((chat_id, messages))
            return [{"id": f"push-{idx}"} for idx, _ in enumerate(messages)]

    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter._client = FakeLineClient()
    adapter.public_base_url = "https://line.example.com"
    monkeypatch.setattr(
        line_adapter,
        "_line_media_allowed_roots",
        lambda: {tmp_path.resolve(), cache_dir.resolve()},
    )
    monkeypatch.setattr(line_adapter, "_line_media_cache_dir", lambda: cache_dir)

    request_id = adapter._cache.register_pending("U-test")
    adapter._pending_buttons["U-test"] = request_id
    adapter._cache.set_ready(request_id, f"好了\n\nMEDIA:{image_path}")

    await adapter._handle_postback_event(
        {
            "replyToken": "reply-token",
            "source": {"type": "user", "userId": "U-test"},
            "postback": {
                "data": '{"action":"show_response","request_id":"%s"}' % request_id
            },
        }
    )

    assert len(adapter._client.replies) == 1
    _reply_token, messages = adapter._client.replies[0]
    assert _reply_token == "reply-token"
    assert [message["type"] for message in messages] == ["text", "image"]
    image_url = messages[1]["originalContentUrl"]
    assert image_url.startswith("https://line.example.com/line/media/")
    token = adapter._media_token_from_url(image_url)
    served_path, _expires_at = adapter._media_tokens[token]
    assert Path(served_path).resolve() == image_path.resolve()
    assert adapter._cache.get(request_id).state is line_adapter.State.DELIVERED
    assert "U-test" not in adapter._pending_buttons


@pytest.mark.asyncio
async def test_line_pending_postback_caches_image_file_until_tap(monkeypatch, tmp_path):
    image_path = tmp_path / "reaction.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    cache_dir = tmp_path / "line-media"

    class FakeLineClient:
        def __init__(self):
            self.replies = []
            self.pushes = []

        async def reply(self, reply_token, messages):
            self.replies.append((reply_token, messages))
            return [{"id": f"reply-{idx}"} for idx, _ in enumerate(messages)]

        async def push(self, chat_id, messages):
            self.pushes.append((chat_id, messages))
            return [{"id": f"push-{idx}"} for idx, _ in enumerate(messages)]

    adapter = line_adapter.LineAdapter(SimpleNamespace(extra={}))
    adapter._client = FakeLineClient()
    adapter.public_base_url = "https://line.example.com"
    monkeypatch.setattr(
        line_adapter,
        "_line_media_allowed_roots",
        lambda: {tmp_path.resolve(), cache_dir.resolve()},
    )
    monkeypatch.setattr(line_adapter, "_line_media_cache_dir", lambda: cache_dir)

    request_id = adapter._cache.register_pending("U-test")
    adapter._pending_buttons["U-test"] = request_id

    result = await adapter.send_image_file("U-test", str(image_path))

    assert result.success
    assert result.message_id == request_id
    assert adapter._client.replies == []
    assert adapter._client.pushes == []
    entry = adapter._cache.get(request_id)
    assert entry.state is line_adapter.State.READY
    assert f"MEDIA:{image_path}" in entry.payload

    await adapter._handle_postback_event(
        {
            "replyToken": "reply-token",
            "source": {"type": "user", "userId": "U-test"},
            "postback": {
                "data": '{"action":"show_response","request_id":"%s"}' % request_id
            },
        }
    )

    assert len(adapter._client.replies) == 1
    assert adapter._client.replies[0][1][0]["type"] == "image"
