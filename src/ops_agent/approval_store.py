"""Persistent one-time approval tokens for publish actions.

The CLI issues tokens with an approver secret; publish consumes them. Tokens are
stored in a JSON file so they survive a new session.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .approvals import ApprovalError


@dataclass
class ApprovalRecord:
    token: str
    product_id: str
    variant_key: str
    action: str
    issued_by: str
    issued_at: str
    expires_at: str
    used: bool = False


class ApprovalStore:
    def __init__(self, path: Path | str, ttl_seconds: int = 86400) -> None:
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds

    def _load(self) -> dict:
        if not self.path.exists():
            return {"tokens": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"tokens": []}
        return payload if isinstance(payload, dict) else {"tokens": []}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def issue(
        self,
        product_id: str,
        variant_key: str,
        issued_by: str,
        action: str = "publish",
        ttl_seconds: int | None = None,
    ) -> ApprovalRecord:
        now = datetime.now(timezone.utc)
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        record = ApprovalRecord(
            token=f"APR-{secrets.token_hex(8).upper()}",
            product_id=str(product_id),
            variant_key=str(variant_key),
            action=action,
            issued_by=issued_by,
            issued_at=now.isoformat(timespec="seconds"),
            expires_at=(now + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
        )
        data = self._load()
        data["tokens"].append(asdict(record))
        self._save(data)
        return record

    def consume(self, token: str, product_id: str, variant_key: str, action: str = "publish") -> ApprovalRecord:
        data = self._load()
        now = datetime.now(timezone.utc)
        for raw in data.get("tokens", []):
            if raw.get("token") != token:
                continue
            try:
                record = ApprovalRecord(**raw)
                expires_at = datetime.fromisoformat(record.expires_at)
            except (TypeError, ValueError) as exc:
                raise ApprovalError("invalid token record") from exc
            if record.used:
                raise ApprovalError("token already used")
            if record.product_id != str(product_id):
                raise ApprovalError("token issued for another product")
            if record.variant_key != str(variant_key):
                raise ApprovalError("token issued for another variant")
            if record.action != action:
                raise ApprovalError("token issued for another action")
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now > expires_at:
                raise ApprovalError("token expired")
            record.used = True
            for index, item in enumerate(data["tokens"]):
                if item.get("token") == token:
                    data["tokens"][index] = asdict(record)
                    break
            try:
                self._save(data)
            except OSError as exc:
                raise ApprovalError("cannot persist approval token state") from exc
            return record
        raise ApprovalError("unknown token")

    def list_active(self) -> list[ApprovalRecord]:
        data = self._load()
        now = datetime.now(timezone.utc)
        active: list[ApprovalRecord] = []
        for raw in data.get("tokens", []):
            try:
                record = ApprovalRecord(**raw)
                expires_at = datetime.fromisoformat(record.expires_at)
            except (TypeError, ValueError):
                continue
            if record.used:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > now:
                active.append(record)
        return active
