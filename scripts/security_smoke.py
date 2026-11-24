"""Smoke-test the OpsPilot console's auth/CSRF/tenant boundaries.

Usage:
    python scripts/security_smoke.py --base-url http://127.0.0.1:8788 \
        --username admin --password "..." [--operator-user operator --operator-password "..."]

It is read-only except for logout/login attempts; it never publishes.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import sys
from urllib.parse import urlparse


class Client:
    def __init__(self, base_url: str) -> None:
        parsed = urlparse(base_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.secure = parsed.scheme == "https"
        self.cookie = ""
        self.csrf = ""

    def request(self, method: str, path: str, body: dict | None = None, csrf: bool = False):
        conn_cls = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        conn = conn_cls(self.host, self.port, timeout=10)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith("opspilot_session="):
            self.cookie = set_cookie.split(";", 1)[0]
        content_type = response.getheader("Content-Type", "")
        conn.close()
        if "application/json" in content_type:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                payload = {}
        else:
            payload = raw.decode("utf-8", errors="replace")
        return response.status, payload, dict(response.getheaders())

    def login(self, username: str, password: str) -> bool:
        status, payload, _ = self.request(
            "POST", "/api/login", {"username": username, "password": password}
        )
        if status != 200:
            return False
        self.csrf = payload.get("csrf", "")
        return True


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(("[PASS] " if condition else "[FAIL] ") + name + (f" — {detail}" if detail else ""))
    return condition


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=None)
    parser.add_argument("--operator-user", default=None)
    parser.add_argument("--operator-password", default=None)
    args = parser.parse_args()
    password = args.password or getpass.getpass("admin password: ")

    results = []
    admin = Client(args.base_url)
    tenant = ""

    # Unauthenticated
    status, _, _ = admin.request("GET", "/api/me")
    results.append(check("未登录 /api/me 返回 401", status == 401, str(status)))

    # Login
    results.append(check("admin 登录成功", admin.login(args.username, password)))
    status, payload, _ = admin.request("GET", "/api/me")
    results.append(check("登录后 /api/me 200", status == 200 and payload.get("user", {}).get("role") == "admin"))

    # CSRF
    status, payload, _ = admin.request("POST", "/api/logout", {})
    results.append(check("缺 CSRF 的 POST 返回 403", status == 403, payload.get("error", "")))

    # Tenant list + overview
    status, payload, _ = admin.request("GET", "/api/tenants")
    results.append(check("租户列表 200", status == 200 and len(payload.get("tenants", [])) >= 1))
    if status == 200 and payload.get("tenants"):
        tenant = payload["tenants"][0]["tenant_id"]
        status, payload, _ = admin.request("GET", f"/api/tenants/{tenant}/overview")
        results.append(check("总览 200", status == 200 and "task_orders" in payload))

    # Raw path traversal (http.client sends ../ verbatim)
    status, _, _ = admin.request("GET", "/api/tenants/../overview")
    results.append(check("租户路径穿越 404", status == 404, str(status)))

    # Audit + CSV export
    status, payload, _ = admin.request("GET", "/api/audit?limit=5")
    results.append(check("审计查询 200", status == 200 and "logs" in payload))
    status, payload, headers = admin.request("GET", "/api/audit/export")
    ok_export = status == 200 and isinstance(payload, str) and "login" in payload
    results.append(check("审计 CSV 导出 200", ok_export, headers.get("Content-Type", "")))

    # Wrong password
    bad = Client(args.base_url)
    status, payload, _ = bad.request(
        "POST", "/api/login", {"username": args.username, "password": "definitely-wrong"}
    )
    results.append(check("错误密码 401", status == 401, str(status)))

    # Optional operator boundaries
    if args.operator_user and args.operator_password:
        operator = Client(args.base_url)
        results.append(check("operator 登录成功", operator.login(args.operator_user, args.operator_password)))
        status, payload, _ = operator.request("GET", "/api/users")
        results.append(check("operator 列用户 403", status == 403, payload.get("error", "")))
        status, payload, _ = operator.request(
            "POST",
            f"/api/tenants/{tenant}/approve",
            {"product_id": "x", "variant_key": "y"},
            csrf=True,
        )
        results.append(check("operator 签发令牌 403", status == 403, payload.get("error", "")))

    print()
    print(f"{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
