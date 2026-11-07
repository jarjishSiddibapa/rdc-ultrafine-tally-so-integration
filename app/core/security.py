"""Password hashing / policy, opaque session tokens, CSRF tokens, in-memory rate limiter."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
# Verified against when the account does not exist, so response time doesn't reveal valid emails.
_DUMMY_HASH = _hasher.hash("not-a-real-password-" + secrets.token_hex(8))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def burn_password_check(password: str) -> None:
    verify_password(password, None)


_COMMON = {
    "password", "password1", "password123", "qwerty123", "welcome123", "admin123", "letmein123",
    "123456789", "1234567890", "iloveyou1", "changeme123",
}


def password_problems(password: str, email: str = "", name: str = "") -> list[str]:
    """Policy for *new* passwords. Returns human-readable problems (empty = acceptable)."""
    problems: list[str] = []
    if len(password) < 10:
        problems.append("Use at least 10 characters.")
    if len(password) > 128:
        problems.append("Use at most 128 characters.")
    if not re.search(r"[a-z]", password) or not re.search(r"[A-Z]", password):
        problems.append("Mix upper and lower case letters.")
    if not re.search(r"\d", password):
        problems.append("Include at least one digit.")
    if password.lower() in _COMMON:
        problems.append("That password is too common.")
    local = email.split("@")[0].lower() if email else ""
    if local and len(local) >= 4 and local in password.lower():
        problems.append("Do not include your email name in the password.")
    return problems


# --------------------------------------------------------------------------- sessions / csrf
def new_session_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_csrf_seed() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_for(seed: str) -> str:
    """Form token = HMAC(secret, seed). The seed lives in an HttpOnly cookie (double-submit, signed)."""
    key = get_settings().secret_key.encode()
    return hmac.new(key, seed.encode(), hashlib.sha256).hexdigest()


def csrf_valid(seed: str | None, submitted: str | None) -> bool:
    if not seed or not submitted:
        return False
    return hmac.compare_digest(csrf_token_for(seed), submitted)


# --------------------------------------------------------------------------- rate limiter
class SlidingWindowLimiter:
    """Small in-process limiter (per worker). Good first line; DB lockout is the durable one."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v][:5000]:
                    self._hits.pop(k, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = SlidingWindowLimiter()
