"""In-memory-only key vault for development-mode Telegram ingestion.

Design rules:

* The raw key is never written to disk, never put in a log line and never
  echoed back to the chat.  ``repr``/``str`` only expose provider names.
* Entries expire (default 1 h) and can be purged explicitly.
* This is a *development convenience*: production should use the OS keyring or
  a secrets manager, not chat.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Conservative patterns: long, high-entropy tokens with no whitespace.
_KEY_PATTERNS = (
    re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$"),                 # OpenAI-style
    re.compile(r"^sk-ant-[A-Za-z0-9_\-]{16,}$"),             # Anthropic
    re.compile(r"^hf_[A-Za-z0-9]{20,}$"),                    # Hugging Face
    re.compile(r"^AIza[0-9A-Za-z_\-]{30,}$"),                # Google
    re.compile(r"^0x[0-9a-fA-F]{64}$"),                      # hex private key
    re.compile(r"^[A-Za-z0-9_\-]{32,}$"),                    # generic long token
)


def looks_like_key(text: str) -> bool:
    t = (text or "").strip()
    if not t or any(ch.isspace() for ch in t):
        return False
    return any(p.match(t) for p in _KEY_PATTERNS)


def parse_key_command(text: str) -> Optional[Tuple[str, str]]:
    """Parse ``/key <provider> <key>``, ``/key <key>`` or a bare key.

    Returns ``(provider, key)`` or ``None``.  The key is returned verbatim and
    must never be logged.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    parts = raw.split()
    if parts[0].lower() == "/key":
        if len(parts) == 2:
            provider, key = "default", parts[1]
        elif len(parts) >= 3:
            provider, key = parts[1], parts[2]
        else:
            return None
        return (provider, key) if looks_like_key(key) else None
    if looks_like_key(raw):
        return "default", raw
    return None


def redact(key: str) -> str:
    if not key:
        return "<empty>"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


@dataclass
class _Entry:
    value: str
    expires_at: float


class InMemoryKeyVault:
    """Thread-safe, process-local, TTL-bounded key store."""

    def __init__(self, ttl_s: float = 3600.0, now_fn=time.time):
        self.ttl_s = float(ttl_s)
        self.now = now_fn
        self._lock = threading.RLock()
        self._data: Dict[str, _Entry] = {}

    def put(self, provider: str, key: str, ttl_s: Optional[float] = None) -> None:
        if not key:
            return
        ttl = self.ttl_s if ttl_s is None else float(ttl_s)
        with self._lock:
            self._data[str(provider)] = _Entry(str(key), self.now() + ttl)

    def get(self, provider: str = "default") -> Optional[str]:
        with self._lock:
            e = self._data.get(str(provider))
            if e is None:
                return None
            if e.expires_at <= self.now():
                self._data.pop(str(provider), None)
                return None
            return e.value

    def purge(self) -> int:
        with self._lock:
            now = self.now()
            dead = [k for k, e in self._data.items() if e.expires_at <= now]
            for k in dead:
                self._data.pop(k, None)
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def providers(self) -> List[str]:
        with self._lock:
            return sorted(self._data)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __repr__(self) -> str:
        return f"InMemoryKeyVault(providers={self.providers()}, ttl_s={self.ttl_s})"

    __str__ = __repr__
