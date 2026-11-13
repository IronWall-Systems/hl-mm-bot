"""Dev-mode Telegram key ingestion: immediate deletion, memory-only storage."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import telegram_monitor as tm  # noqa: E402
from mmbot.learning.keys import parse_key_command  # noqa: E402

KEY = "sk-" + "a" * 24


def _update(text: str, user_id: int = 42, chat_id: int = 99, mid: int = 7):
    return {"message": {"from": {"id": user_id}, "chat": {"id": chat_id},
                        "message_id": mid, "text": text}}


def test_parse_key_command_forms():
    assert parse_key_command(f"/key openai {KEY}") == ("openai", KEY)
    assert parse_key_command(f"/key {KEY}") == ("default", KEY)
    assert parse_key_command(KEY) == ("default", KEY)
    assert parse_key_command("hello world") is None
    assert parse_key_command("/key too short") is None


def test_key_message_deleted_then_kept_in_memory(monkeypatch):
    calls = []

    def fake_api(method, payload=None, timeout=35.0):
        calls.append((method, dict(payload or {})))
        return {"ok": True, "result": {"message_id": 1}}

    monkeypatch.setattr(tm, "ALLOW_KEY_INGEST", True)
    monkeypatch.setattr(tm, "AUTH_USER_IDS", {42})
    monkeypatch.setattr(tm, "_api", fake_api)
    tm.KEY_VAULT.clear()

    assert tm.handle_update(_update(f"/key openai {KEY}")) is True

    methods = [m for m, _ in calls]
    assert "deleteMessage" in methods and "sendMessage" in methods
    assert methods.index("deleteMessage") < methods.index("sendMessage")
    dm = next(p for m, p in calls if m == "deleteMessage")
    assert dm == {"chat_id": 99, "message_id": 7}
    assert tm.KEY_VAULT.get("openai") == KEY
    # The raw key must never be echoed back to the chat.
    assert all(KEY not in str(p) for _, p in calls)


def test_bare_key_uses_default_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(tm, "ALLOW_KEY_INGEST", True)
    monkeypatch.setattr(tm, "AUTH_USER_IDS", {42})
    monkeypatch.setattr(tm, "_api",
                        lambda method, payload=None, timeout=35.0:
                        calls.append((method, dict(payload or {}))) or {"ok": True})
    tm.KEY_VAULT.clear()
    assert tm.handle_update(_update(KEY)) is True
    assert tm.KEY_VAULT.get("default") == KEY


def test_ingest_disabled_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(tm, "ALLOW_KEY_INGEST", False)
    monkeypatch.setattr(tm, "AUTH_USER_IDS", {42})
    monkeypatch.setattr(tm, "_api",
                        lambda method, payload=None, timeout=35.0:
                        calls.append(method) or {"ok": True})
    tm.KEY_VAULT.clear()
    assert tm.handle_update(_update(KEY)) is False
    assert calls == []
    assert tm.KEY_VAULT.get("default") is None


def test_key_not_stored_when_delete_fails(monkeypatch):
    calls = []

    def fake_api(method, payload=None, timeout=35.0):
        calls.append((method, dict(payload or {})))
        if method == "deleteMessage":
            return {"ok": False, "description": "message can't be deleted"}
        return {"ok": True}

    monkeypatch.setattr(tm, "ALLOW_KEY_INGEST", True)
    monkeypatch.setattr(tm, "AUTH_USER_IDS", {42})
    monkeypatch.setattr(tm, "_api", fake_api)
    tm.KEY_VAULT.clear()

    assert tm.handle_update(_update(f"/key openai {KEY}")) is True
    assert tm.KEY_VAULT.get("openai") is None
    assert len(tm.KEY_VAULT) == 0
    assert all(KEY not in str(p) for _, p in calls)


def test_unauthorized_user_cannot_ingest(monkeypatch):
    calls = []
    monkeypatch.setattr(tm, "ALLOW_KEY_INGEST", True)
    monkeypatch.setattr(tm, "AUTH_USER_IDS", {42})
    monkeypatch.setattr(tm, "_api",
                        lambda method, payload=None, timeout=35.0:
                        calls.append(method) or {"ok": True})
    tm.KEY_VAULT.clear()
    assert tm.handle_update(_update(KEY, user_id=7)) is False
    assert calls == []
    assert len(tm.KEY_VAULT) == 0
