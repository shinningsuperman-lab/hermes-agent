from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.platforms.base import SessionSource
from gateway.run import GatewayRunner


def _runner():
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner


def test_line_allowed_group_authorizes_non_owner_sender(monkeypatch):
    monkeypatch.delenv("LINE_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uowner")
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Catv")

    source = SessionSource(
        platform=Platform("line"),
        chat_id="Catv",
        chat_type="group",
        user_id="Uother",
        user_name="group member",
    )

    assert _runner()._is_user_authorized(source) is True


def test_line_group_allowlist_does_not_authorize_unknown_dm(monkeypatch):
    monkeypatch.delenv("LINE_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uowner")
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Catv")

    source = SessionSource(
        platform=Platform("line"),
        chat_id="Uother",
        chat_type="dm",
        user_id="Uother",
        user_name="unknown dm",
    )

    assert _runner()._is_user_authorized(source) is False
