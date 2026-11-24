"""OpsPilot console authentication: password hashing, sessions, login limits."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class UserStore:
    def __init__(
        self,
        path: Path | str | None = None,
        users: dict | None = None,
        iterations: int = PBKDF2_ITERATIONS,
    ) -> None:
        self.path = Path(path) if path else None
        self.iterations = iterations
        self.users: dict[str, dict] = dict(users or {})
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.users = {}
            return
        records = payload.get("users", []) if isinstance(payload, dict) else []
        self.users = {str(item["username"]): item for item in records if item.get("username")}

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"users": list(self.users.values())}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, username: str, password: str, role: str, tenants: list[str], display_name: str = "") -> dict:
        username = username.strip().lower()
        if not username or username in self.users:
            raise ValueError("username already exists")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        if role not in ("admin", "approver", "operator"):
            raise ValueError("invalid role")
        record = {
            "username": username,
            "display_name": display_name or username,
            "role": role,
            "tenants": list(tenants),
            "password_hash": hash_password(password, self.iterations),
        }
        self.users[username] = record
        self.save()
        return record

    def get(self, username: str) -> dict | None:
        return self.users.get(str(username).lower())

    def authenticate(self, username: str, password: str) -> dict | None:
        record = self.get(username)
        if not record or record.get("disabled"):
            return None
        if not verify_password(password, str(record.get("password_hash", ""))):
            return None
        return record

    def list_public(self) -> list[dict]:
        return [self.public(record) for record in sorted(self.users.values(), key=lambda item: item["username"])]

    @staticmethod
    def public(record: dict) -> dict:
        return {
            "username": record["username"],
            "display_name": record.get("display_name") or record["username"],
            "role": record["role"],
            "tenants": list(record.get("tenants") or []),
            "disabled": bool(record.get("disabled", False)),
        }

    def _active_admins(self) -> int:
        return sum(
            1
            for record in self.users.values()
            if record.get("role") == "admin" and not record.get("disabled")
        )

    def update(
        self,
        username: str,
        *,
        role: str | None = None,
        tenants: list[str] | None = None,
        disabled: bool | None = None,
        display_name: str | None = None,
    ) -> dict:
        record = self.get(username)
        if record is None:
            raise ValueError("user not found")
        new_role = role if role is not None else record["role"]
        if new_role not in ("admin", "approver", "operator"):
            raise ValueError("invalid role")
        new_disabled = bool(record.get("disabled", False)) if disabled is None else bool(disabled)
        loses_admin = record.get("role") == "admin" and not record.get("disabled") and (
            new_role != "admin" or new_disabled
        )
        if loses_admin and self._active_admins() <= 1:
            raise ValueError("不能停用或降级最后一个管理员")
        record["role"] = new_role
        if tenants is not None:
            record["tenants"] = [str(item) for item in tenants]
        record["disabled"] = new_disabled
        if display_name is not None:
            record["display_name"] = display_name or record["username"]
        self.save()
        return record

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        record = self.get(username)
        if record is None:
            raise ValueError("user not found")
        record["password_hash"] = hash_password(password, self.iterations)
        self.save()


@dataclass
class Session:
    token: str
    username: str
    csrf: str
    expires_at: float


class SessionManager:
    def __init__(self, ttl_seconds: int = 28800) -> None:
        self.ttl_seconds = ttl_seconds
        self.sessions: dict[str, Session] = {}

    def create(self, username: str) -> Session:
        session = Session(
            token=secrets.token_urlsafe(32),
            username=username,
            csrf=secrets.token_urlsafe(24),
            expires_at=time.time() + self.ttl_seconds,
        )
        self.sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self.sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self.sessions.pop(token, None)
            return None
        return session

    def destroy(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    def destroy_user(self, username: str) -> None:
        for token, session in list(self.sessions.items()):
            if session.username == username:
                self.sessions.pop(token, None)


class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        items = [item for item in self.failures.get(key, []) if now - item < self.window_seconds]
        self.failures[key] = items
        return items

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_failures

    def fail(self, key: str) -> None:
        values = self._recent(key)
        values.append(time.monotonic())
        self.failures[key] = values

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)
