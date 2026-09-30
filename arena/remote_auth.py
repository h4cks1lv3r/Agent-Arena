"""Single-operator remote access for an HTTPS reverse proxy on loopback.

Access secrets are never sessions. Sessions live only in memory and expire on
restart. No forwarded identity header is trusted by this module.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import ipaddress
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import urlsplit


COOKIE_NAME = "__Host-arena_session"
SESSION_SECONDS = 12 * 60 * 60


def validate_public_origin(value):
    message = "Public origin must be an HTTPS origin without a path, query, fragment, or user information."
    if (not isinstance(value, str) or not value or '?' in value or '#' in value
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None or parsed.netloc.endswith(":")):
            raise ValueError()
        hostname, port = parsed.hostname, parsed.port
        if not hostname or not hostname.isascii() or "%" in hostname:
            raise ValueError()
        if ":" in hostname:
            host = "[" + str(ipaddress.IPv6Address(hostname)) + "]"
        else:
            if len(hostname) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in hostname.split(".")):
                raise ValueError()
            host = hostname.lower()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        return "https://" + host + (f":{port}" if port not in (None, 443) else "")
    except (ValueError, TypeError):
        raise ValueError(message) from None


def initialize_access(data_dir):
    """Create, never overwrite, a private high-entropy access secret."""
    path = Path(data_dir) / "access-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("An access-token file already exists. It was not changed or displayed.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return token


def load_access_secret(data_dir, environ=None):
    environ = os.environ if environ is None else environ
    if "ARENA_ACCESS_TOKEN" in environ:
        token = environ["ARENA_ACCESS_TOKEN"]
    else:
        path = Path(data_dir) / "access-token"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Access-token must be a regular private file.")
                if os.name != "nt" and metadata.st_mode & 0o077:
                    raise ValueError("Access-token permissions must be 0600. Restrict this file before remote startup.")
                token = handle.read(4097).strip()
        except (OSError, UnicodeError):
            raise ValueError("Remote access requires ARENA_ACCESS_TOKEN or a private access-token file. Run --init-access first.") from None
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or any(ord(c) < 32 or ord(c) == 127 for c in token):
        raise ValueError("Remote access requires an access token of at least 32 characters, without control characters.")
    return token


class AuthFailure(ValueError):
    def __init__(self, message, status=401, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Session:
    csrf: str
    expires: float


class RemoteAuth:
    def __init__(self, secret, *, clock=time.time, attempts=5, window=60, max_sessions=64, max_peers=256):
        if not isinstance(secret, str) or not 32 <= len(secret) <= 4096:
            raise ValueError("Remote access secret is missing or too short.")
        self._secret_digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._clock = clock
        self._attempts, self._window = attempts, window
        self._max_sessions, self._max_peers = max_sessions, max_peers
        self._sessions = OrderedDict()
        self._peers = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode("utf-8")).digest()

    def _expire(self, stamp):
        for key, session in list(self._sessions.items()):
            if session.expires <= stamp:
                self._sessions.pop(key, None)
        for peer, attempts in list(self._peers.items()):
            while attempts and attempts[0] <= stamp - self._window:
                attempts.popleft()
            if not attempts:
                self._peers.pop(peer, None)

    def login(self, supplied, peer):
        # peer is the TCP peer from the server, never X-Forwarded-For.
        with self._lock:
            stamp = self._clock()
            self._expire(stamp)
            if peer not in self._peers and len(self._peers) >= self._max_peers:
                self._peers.popitem(last=False)
            attempts = self._peers.setdefault(peer, deque())
            if len(attempts) >= self._attempts:
                retry = max(1, math.ceil(attempts[0] + self._window - stamp))
                raise AuthFailure("Too many sign-in attempts. Try again shortly.", 429, retry)
            attempts.append(stamp)
            valid_shape = isinstance(supplied, str) and len(supplied) <= 4096
            candidate = self._digest(supplied if valid_shape else "")
            if not hmac.compare_digest(candidate, self._secret_digest) or not valid_shape:
                raise AuthFailure("The access token was not accepted.")
            self._peers.pop(peer, None)
            while len(self._sessions) >= self._max_sessions:
                self._sessions.popitem(last=False)
            cookie = secrets.token_urlsafe(32)
            session = Session(csrf=secrets.token_urlsafe(32), expires=stamp + SESSION_SECONDS)
            self._sessions[self._digest(cookie)] = session
            return cookie, session

    @staticmethod
    def cookie_value(header):
        if not isinstance(header, str) or len(header) > 4096:
            return None
        # Reject ambiguous duplicate session cookies instead of picking a value.
        if len(re.findall(r"(?:^|;)\s*" + re.escape(COOKIE_NAME) + r"\s*=", header)) != 1:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(header)
            token = cookies[COOKIE_NAME].value
            return token if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else None
        except (CookieError, KeyError):
            return None

    def session(self, header):
        token = self.cookie_value(header)
        if token is None:
            return None
        with self._lock:
            self._expire(self._clock())
            return self._sessions.get(self._digest(token))

    @staticmethod
    def csrf_valid(session, supplied):
        return isinstance(supplied, str) and len(supplied) <= 256 and hmac.compare_digest(session.csrf.encode(), supplied.encode())

    def logout(self, header):
        token = self.cookie_value(header)
        if token is not None:
            with self._lock:
                self._sessions.pop(self._digest(token), None)
