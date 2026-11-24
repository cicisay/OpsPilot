"""SQLite audit log for the OpsPilot console.

Every security-relevant action is recorded with actor, action, tenant, target,
IP and result. Passwords and tokens are never written here.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditDB:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    tenant TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '',
                    ip TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ok',
                    detail TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts ON audit(tenant, id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit(action, id)")

    def record(
        self,
        actor: str,
        action: str,
        tenant: str = "",
        target: str = "",
        ip: str = "",
        status: str = "ok",
        detail: str = "",
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit(ts, actor, action, tenant, target, ip, status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, actor, action, tenant, target, ip, status, detail[:500]),
            )

    def query(self, limit: int = 100, tenant: str | None = None, action: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        return self._query(limit, tenant, action)

    def export(self, tenant: str | None = None, limit: int = 5000) -> list[dict]:
        limit = max(1, min(int(limit), 5000))
        return self._query(limit, tenant, None)

    def _query(self, limit: int, tenant: str | None, action: str | None) -> list[dict]:
        clauses = []
        values: list[str | int] = []
        if tenant:
            clauses.append("tenant = ?")
            values.append(tenant)
        if action:
            clauses.append("action = ?")
            values.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM audit {where} ORDER BY id DESC LIMIT ?"
        values.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
