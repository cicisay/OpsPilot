"""Tenant registry for the OpsPilot console."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass
class Tenant:
    tenant_id: str
    name: str
    site_base_url: str
    token_env: str
    data_dir: Path
    currency: str = "USD"
    timezone: str = "Asia/Shanghai"
    platform: str = "selfhosted"


class TenantStore:
    def __init__(self, path: Path | str, base_dir: Path | str) -> None:
        self.path = Path(path)
        self.base_dir = Path(base_dir)
        self.tenants: dict[str, Tenant] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.tenants = {}
            return
        for raw in payload.get("tenants", []) if isinstance(payload, dict) else []:
            tenant_id = str(raw.get("tenant_id", ""))
            if not TENANT_ID_RE.fullmatch(tenant_id):
                continue
            data_dir = raw.get("data_dir")
            resolved = (
                self.base_dir / str(data_dir)
                if data_dir
                else self.base_dir / "tenants" / tenant_id
            )
            self.tenants[tenant_id] = Tenant(
                tenant_id=tenant_id,
                name=str(raw.get("name", tenant_id)),
                site_base_url=str(raw.get("site_base_url", "")),
                token_env=str(raw.get("token_env", "")),
                data_dir=resolved,
                currency=str(raw.get("currency", "USD")),
                timezone=str(raw.get("timezone", "Asia/Shanghai")),
                platform=str(raw.get("platform", "selfhosted")),
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tenants": [
            {
                "tenant_id": tenant.tenant_id,
                "name": tenant.name,
                "site_base_url": tenant.site_base_url,
                "token_env": tenant.token_env,
                "data_dir": str(tenant.data_dir.relative_to(self.base_dir)).replace("\\", "/"),
                "currency": tenant.currency,
                "timezone": tenant.timezone,
                "platform": tenant.platform,
            }
            for tenant in self.tenants.values()
        ]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, tenant: Tenant) -> None:
        if not TENANT_ID_RE.fullmatch(tenant.tenant_id):
            raise ValueError("invalid tenant id")
        self.tenants[tenant.tenant_id] = tenant
        self.save()

    def get(self, tenant_id: str) -> Tenant | None:
        return self.tenants.get(str(tenant_id))

    def all(self) -> list[Tenant]:
        return sorted(self.tenants.values(), key=lambda item: item.tenant_id)

    def for_user(self, user: dict) -> list[Tenant]:
        if user.get("role") == "admin":
            return self.all()
        allowed = set(user.get("tenants") or [])
        return [tenant for tenant in self.all() if tenant.tenant_id in allowed]

    def data_file(self, tenant_id: str, filename: str) -> Path:
        tenant = self.get(tenant_id)
        if tenant is None:
            raise KeyError(tenant_id)
        if "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError("invalid data filename")
        return tenant.data_dir / filename

    @staticmethod
    def to_public(tenant: Tenant) -> dict:
        data = asdict(tenant)
        data["data_dir"] = str(tenant.data_dir)
        return data
