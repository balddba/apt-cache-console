"""Bounded, process-local browser sessions for the dashboard."""

import secrets
import time
from threading import Lock

COOKIE_NAME = "squid_ui_session"
SESSION_SECONDS = 12 * 60 * 60


class Sessions:
    """Keep opaque sessions until logout, expiry, or application restart."""

    def __init__(self) -> None:
        """Initialize a bounded session registry shared by request threads."""
        self._tokens: dict[str, float] = {}
        self._lock = Lock()

    def create(self) -> str:
        """Issue a random session, pruning expired and oldest excess sessions."""
        now = time.time()
        with self._lock:
            self._tokens = {
                token: expiry for token, expiry in self._tokens.items() if expiry > now
            }
            if len(self._tokens) >= 1024:
                del self._tokens[next(iter(self._tokens))]
            token = secrets.token_urlsafe(nbytes=32)
            self._tokens[token] = now + SESSION_SECONDS
            return token

    def valid(self, token: str | None) -> bool:
        """Accept only known, unexpired session cookies."""
        with self._lock:
            if token is None or self._tokens.get(token, 0) <= time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def discard(self, token: str | None) -> None:
        """Revoke a session immediately, including captured copies of its cookie."""
        with self._lock:
            self._tokens.pop(token, None)
