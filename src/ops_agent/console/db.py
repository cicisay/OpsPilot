"""SQLite-backed console stores: users, tenant memberships and tenants.

The JSON stores remain for backward compatibility; the console can run against
this database with ``console serve --db``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .auth import PBKDF2_ITERATIONS, hash_password, verify_password
from .tenants import TENANT_ID_RE, Tenant

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_tenants (
    username TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    PRIMARY KEY (username, tenant_id)
);
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    site_base_url TEXT NOT NULL DEFAULT '',
    token_env TEXT NOT NULL DEFAULT '',
    data_dir TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    platform TEXT NOT NULL DEFAULT 'selfhosted'
);
"""


class ConsoleDB:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(SCHEMA)
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tenants)")}
            if "platform" not in columns:
                self.conn.execute("ALTER TABLE tenants ADD COLUMN platform TEXT NOT NULL DEFAULT 'selfhosted'")

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class SQLiteUserStore:
    def __init__(self, db: ConsoleDB, iterations: int = PBKDF2_ITERATIONS) -> None:
        self.db = db
        self.iterations = iterations

    @property
    def users(self) -> dict[str, dict]:
        with self.db.lock:
            rows = self.db.conn.execute("SELECT username FROM users").fetchall()
        return {row["username"]: self.get(row["username"]) for row in rows}

    def _memberships(self, username: str) -> list[str]:
        with self.db.lock:
            rows = self.db.conn.execute(
                "SELECT tenant_id FROM user_tenants WHERE username = ? ORDER BY tenant_id",
                (username,),
            ).fetchall()
        return [row["tenant_id"] for row in rows]

    def get(self, username: str) -> dict | None:
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT * FROM users WHERE username = ?", (str(username).lower(),)
            ).fetchone()
        if row is None:
            return None
        return {
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "password_hash": row["password_hash"],
            "disabled": bool(row["disabled"]),
            "tenants": self._memberships(row["username"]),
        }

    def authenticate(self, username: str, password: str) -> dict | None:
        record = self.get(username)
        if not record or record.get("disabled"):
            return None
        if not verify_password(password, str(record.get("password_hash", ""))):
            return None
        return record

    def _active_admins(self) -> int:
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND disabled = 0"
            ).fetchone()
        return int(row["count"])

    def add(self, username: str, password: str, role: str, tenants: list[str], display_name: str = "") -> dict:
        username = username.strip().lower()
        if not username:
            raise ValueError("username is required")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        if role not in ("admin", "approver", "operator"):
            raise ValueError("invalid role")
        if self.get(username) is not None:
            raise ValueError("username already exists")
        encoded = hash_password(password, self.iterations)
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "INSERT INTO users(username, display_name, role, password_hash, disabled) "
                "VALUES (?, ?, ?, ?, 0)",
                (username, display_name or username, role, encoded),
            )
            for tenant_id in tenants:
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO user_tenants(username, tenant_id) VALUES (?, ?)",
                    (username, str(tenant_id)),
                )
        return self.get(username)  # type: ignore[return-value]

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
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "UPDATE users SET role = ?, disabled = ?, display_name = ? WHERE username = ?",
                (
                    new_role,
                    1 if new_disabled else 0,
                    (display_name if display_name is not None else record.get("display_name")) or username,
                    username,
                ),
            )
            if tenants is not None:
                self.db.conn.execute("DELETE FROM user_tenants WHERE username = ?", (username,))
                for tenant_id in tenants:
                    self.db.conn.execute(
                        "INSERT OR IGNORE INTO user_tenants(username, tenant_id) VALUES (?, ?)",
                        (username, str(tenant_id)),
                    )
        return self.get(username)  # type: ignore[return-value]

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        if self.get(username) is None:
            raise ValueError("user not found")
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(password, self.iterations), username),
            )

    def list_public(self) -> list[dict]:
        with self.db.lock:
            rows = self.db.conn.execute("SELECT username FROM users ORDER BY username").fetchall()
        return [self.public(self.get(row["username"])) for row in rows]

    @staticmethod
    def public(record: dict) -> dict:
        return {
            "username": record["username"],
            "display_name": record.get("display_name") or record["username"],
            "role": record["role"],
            "tenants": list(record.get("tenants") or []),
            "disabled": bool(record.get("disabled", False)),
        }

    def save(self) -> None:
        return None


class SQLiteTenantStore:
    def __init__(self, db: ConsoleDB, base_dir: Path | str) -> None:
        self.db = db
        self.base_dir = Path(base_dir)

    def _row(self, row: sqlite3.Row) -> Tenant:
        raw = Path(row["data_dir"])
        data_dir = raw if raw.is_absolute() else self.base_dir / raw
        return Tenant(
            tenant_id=row["tenant_id"],
            name=row["name"],
            site_base_url=row["site_base_url"],
            token_env=row["token_env"],
            data_dir=data_dir,
            currency=row["currency"],
            timezone=row["timezone"],
            platform=row["platform"],
        )

    @property
    def tenants(self) -> dict[str, Tenant]:
        return {tenant.tenant_id: tenant for tenant in self.all()}

    def get(self, tenant_id: str) -> Tenant | None:
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (str(tenant_id),)
            ).fetchone()
        return self._row(row) if row else None

    def all(self) -> list[Tenant]:
        with self.db.lock:
            rows = self.db.conn.execute("SELECT * FROM tenants ORDER BY tenant_id").fetchall()
        return [self._row(row) for row in rows]

    def for_user(self, user: dict) -> list[Tenant]:
        if user.get("role") == "admin":
            return self.all()
        allowed = list(user.get("tenants") or [])
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        with self.db.lock:
            rows = self.db.conn.execute(
                f"SELECT * FROM tenants WHERE tenant_id IN ({placeholders}) ORDER BY tenant_id",
                allowed,
            ).fetchall()
        return [self._row(row) for row in rows]

    def add(self, tenant: Tenant) -> None:
        if not TENANT_ID_RE.fullmatch(tenant.tenant_id):
            raise ValueError("invalid tenant id")
        try:
            data_dir = str(tenant.data_dir.relative_to(self.base_dir)).replace("\\", "/")
        except ValueError:
            data_dir = str(tenant.data_dir)
        with self.db.lock, self.db.conn:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO tenants(tenant_id, name, site_base_url, token_env, data_dir, currency, timezone, platform) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant.tenant_id,
                    tenant.name,
                    tenant.site_base_url,
                    tenant.token_env,
                    data_dir,
                    tenant.currency,
                    tenant.timezone,
                    tenant.platform,
                ),
            )

    def data_file(self, tenant_id: str, filename: str) -> Path:
        tenant = self.get(tenant_id)
        if tenant is None:
            raise KeyError(tenant_id)
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError("invalid data filename")
        return tenant.data_dir / filename

    def save(self) -> None:
        return None

    @staticmethod
    def to_public(tenant: Tenant) -> dict:
        return {
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "site_base_url": tenant.site_base_url,
            "token_env": tenant.token_env,
            "data_dir": str(tenant.data_dir),
            "currency": tenant.currency,
            "timezone": tenant.timezone,
            "platform": tenant.platform,
        }


def migrate_json(db: ConsoleDB, users_path: Path, tenants_path: Path, base_dir: Path) -> tuple[int, int]:
    user_store = SQLiteUserStore(db)
    tenant_store = SQLiteTenantStore(db, base_dir)
    users_count = 0
    tenants_count = 0
    if users_path.exists():
        payload = json.loads(users_path.read_text(encoding="utf-8"))
        for raw in payload.get("users", []):
            if user_store.get(raw["username"]) is None:
                with db.lock, db.conn:
                    db.conn.execute(
                        "INSERT INTO users(username, display_name, role, password_hash, disabled) VALUES (?, ?, ?, ?, ?)",
                        (
                            raw["username"].lower(),
                            raw.get("display_name") or raw["username"],
                            raw["role"],
                            raw["password_hash"],
                            1 if raw.get("disabled") else 0,
                        ),
                    )
                    for tenant_id in raw.get("tenants") or []:
                        db.conn.execute(
                            "INSERT OR IGNORE INTO user_tenants(username, tenant_id) VALUES (?, ?)",
                            (raw["username"].lower(), str(tenant_id)),
                        )
                users_count += 1
    if tenants_path.exists():
        payload = json.loads(tenants_path.read_text(encoding="utf-8"))
        for raw in payload.get("tenants", []):
            tenant_id = raw["tenant_id"]
            data_dir_raw = raw.get("data_dir", f"tenants/{tenant_id}")
            tenant = Tenant(
                tenant_id=tenant_id,
                name=raw["name"],
                site_base_url=raw.get("site_base_url", ""),
                token_env=raw.get("token_env", ""),
                data_dir=(base_dir / data_dir_raw) if not Path(data_dir_raw).is_absolute() else Path(data_dir_raw),
                currency=raw.get("currency", "USD"),
                timezone=raw.get("timezone", "Asia/Shanghai"),
                platform=raw.get("platform", "selfhosted"),
            )
            tenant_store.add(tenant)
            tenants_count += 1
    return users_count, tenants_count
