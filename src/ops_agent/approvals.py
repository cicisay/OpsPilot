"""One-time approval tokens for high-risk write actions."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta


class ApprovalError(Exception):
    """Raised when a token is missing, expired, reused or for another SKU."""


@dataclass
class ApprovalToken:
    token: str
    sku_id: str
    action: str
    reason: str
    issued_at: datetime
    expires_at: datetime
    used: bool = False


class ApprovalService:
    def __init__(self, ttl_seconds: int = 3600, now=None) -> None:
        self._tokens: dict[str, ApprovalToken] = {}
        self.ttl_seconds = ttl_seconds
        self._now = now or datetime.now

    def issue(self, sku_id: str, action: str, reason: str) -> ApprovalToken:
        now = self._now()
        token = ApprovalToken(
            token=f"APR-{secrets.token_hex(4).upper()}",
            sku_id=sku_id,
            action=action,
            reason=reason,
            issued_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
        )
        self._tokens[token.token] = token
        return token

    def validate(self, token: str, sku_id: str, action: str) -> ApprovalToken:
        record = self._tokens.get(token)
        if record is None:
            raise ApprovalError("unknown token")
        if record.used:
            raise ApprovalError("token already used")
        if record.sku_id != sku_id:
            raise ApprovalError("token issued for another SKU")
        if record.action != action:
            raise ApprovalError("token issued for another action")
        if self._now() > record.expires_at:
            raise ApprovalError("token expired")
        return record

    def consume(self, token: str, sku_id: str, action: str) -> ApprovalToken:
        record = self.validate(token, sku_id, action)
        record.used = True
        return record
