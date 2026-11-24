"""Platform connector registry.

Only the self-hosted Agent API is implemented. Shopify / WooCommerce are
accepted as tenant platform values but must fail loudly until their OAuth and
mapping layers are actually built.
"""

from __future__ import annotations

import os

from ..site_platform import SitePlatform
from .tenants import Tenant


class ConnectorError(Exception):
    pass


def build_site(tenant: Tenant, site_factory=None):
    if site_factory is not None:
        return site_factory(tenant)
    platform = (tenant.platform or "selfhosted").lower()
    if platform not in ("selfhosted", "custom", ""):
        raise ConnectorError(
            f"{platform} 连接器尚未实现；当前只支持 selfhosted Agent API"
        )
    token = os.environ.get(tenant.token_env or "", "")
    if not tenant.site_base_url or not token:
        raise ConnectorError("该租户还没有配置站点或 token")
    return SitePlatform(tenant.site_base_url, token)
