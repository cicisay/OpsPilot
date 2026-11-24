"""Minimal stdlib HTTP server for the OpsPilot console."""

from __future__ import annotations

import json
import re
import ssl
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .app import ConsoleApp, ConsoleError

COOKIE_NAME = "opspilot_session"
MAX_BODY = 64 * 1024
STATIC_FILES = {
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class ConsoleHandler(BaseHTTPRequestHandler):
    app: ConsoleApp
    server_version = "OpsPilot/1.0"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        print(f"[console] {self.address_string()} {fmt % args}")

    # ---- response helpers ------------------------------------
    def _headers(self, status: int, content_type: str, length: int, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if getattr(self.server, "scheme", "http") == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, status: int, payload: dict, extra=None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), extra)
        self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, content_type: str, extra=None) -> None:
        self._headers(status, content_type, len(body), extra)
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        # Rejecting a POST before its body is read leaves unread bytes in the
        # socket. Closing then makes Windows send RST, which kills the error
        # response before the client can read it. Drain first, then reply.
        self._drain_body()
        self._json(status, {"ok": False, "error": message})

    def _drain_body(self) -> None:
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if 0 < length <= MAX_BODY:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _file(self, name: str, content_type: str) -> None:
        path = self.app.static_dir / name
        if not path.is_file():
            self._error(404, "not found")
            return
        body = path.read_bytes()
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    # ---- request helpers -------------------------------------
    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _cookie(self, value: str, max_age: int) -> str:
        """Session cookie, with ``Secure`` whenever the server is actually TLS.

        Sending it over plain HTTP would make the flag a lie; omitting it under
        HTTPS is how a session leaks through a downgrade or a mixed-content hop.
        """

        parts = [f"{COOKIE_NAME}={value}", "HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={max_age}"]
        if getattr(self.server, "scheme", "http") == "https":
            parts.append("Secure")
        return "; ".join(parts)

    def _require_session(self):
        token = self._cookie_token()
        user = self.app.session_user(token)
        if user is None:
            raise ConsoleError(401, "请先登录")
        session = self.app.sessions.get(token)
        return session, user

    def _json_body(self) -> dict:
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise ConsoleError(415, "Content-Type 必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ConsoleError(400, "无效的 Content-Length") from None
        if length <= 0 or length > MAX_BODY:
            raise ConsoleError(413, "请求体过大或为空")
        raw = self.rfile.read(length)
        self._body_read = True
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ConsoleError(400, "请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise ConsoleError(400, "请求体必须是对象")
        return payload

    def _check_csrf(self, session) -> None:
        header = self.headers.get("X-CSRF-Token", "")
        if not header or header != session.csrf:
            raise ConsoleError(403, "CSRF 校验失败")

    @staticmethod
    def _int_field(payload: dict, key: str, default: int) -> int:
        try:
            return int(payload.get(key, default))
        except (TypeError, ValueError):
            raise ConsoleError(400, f"{key} 必须是整数") from None

    # ---- HTTP methods ----------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._file("index.html", "text/html; charset=utf-8")
                return
            if path in STATIC_FILES:
                self._file(*STATIC_FILES[path])
                return
            if path == "/api/me":
                session, user = self._require_session()
                self._json(200, {"ok": True, "user": self.app.public_user(user), "csrf": session.csrf})
                return
            if path == "/api/tenants":
                _, user = self._require_session()
                self._json(200, {"ok": True, "tenants": self.app.my_tenants(user)})
                return
            if path == "/api/users":
                _, user = self._require_session()
                self._json(200, {"ok": True, "users": self.app.list_users(user)})
                return
            if path == "/api/audit":
                _, user = self._require_session()
                query = parse_qs(urlparse(self.path).query)
                tenant = (query.get("tenant") or [None])[0]
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    limit = 100
                self._json(200, {"ok": True, "logs": self.app.audit_logs(user, tenant, limit)})
                return
            if path == "/api/audit/export":
                _, user = self._require_session()
                query = parse_qs(urlparse(self.path).query)
                tenant = (query.get("tenant") or [None])[0]
                try:
                    limit = int((query.get("limit") or ["5000"])[0])
                except ValueError:
                    limit = 5000
                csv_text = self.app.export_audit_csv(user, tenant, limit)
                self._bytes(
                    200,
                    csv_text.encode("utf-8-sig"),
                    "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="opspilot-audit.csv"'},
                )
                return
            # 委托台 / 事实面板的 UI 已于 Step 30 下线（只摘前端，能力保留）。
            # 这两组接口仍然可用：对话式助手会直接复用意图层与委托历史，
            # 删除前请先确认 agents/ 与 intake/ 没有调用方，不要当死代码清理。
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/intake/history", path)
            if match:
                _, user = self._require_session()
                self._json(200, {"ok": True, **self.app.intake_history(user, match.group(1))})
                return
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/facts", path)
            if match:
                _, user = self._require_session()
                query = parse_qs(urlparse(self.path).query)
                scope = (query.get("scope") or [""])[0]
                status = (query.get("status") or ["active"])[0]
                text = (query.get("q") or [""])[0]
                try:
                    limit = int((query.get("limit") or ["50"])[0])
                except ValueError:
                    limit = 50
                try:
                    budget = int((query.get("budget") or ["800"])[0])
                except ValueError:
                    budget = 800
                payload = self.app.facts_list(
                    user,
                    match.group(1),
                    scope=scope,
                    status=status,
                    limit=limit,
                    query=text,
                    budget_tokens=budget,
                )
                self._json(200, {"ok": True, **payload})
                return
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/overview", path)
            if match:
                _, user = self._require_session()
                self._json(200, {"ok": True, **self.app.overview(user, match.group(1))})
                return
            # 按需扫描的进度：页面每 1–2 秒问一次，只读状态文件，不碰模型。
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/scan/status", path)
            if match:
                _, user = self._require_session()
                self._json(200, {"ok": True, **self.app.scan_status(user, match.group(1))})
                return
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/memory/([0-9A-Za-z][0-9A-Za-z._-]{0,63})", path)
            if match:
                _, user = self._require_session()
                payload = self.app.memory_replay(user, match.group(1), match.group(2))
                self._json(200, {"ok": True, **payload})
                return
            self._error(404, "not found")
        except ConsoleError as exc:
            self._error(exc.status, exc.message)
        except Exception:
            traceback.print_exc()
            self._error(500, "服务器内部错误")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                payload = self._json_body()
                result = self.app.login(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                    self.client_address[0],
                )
                self._json(
                    200,
                    {"ok": True, "user": result["user"], "csrf": result["csrf"]},
                    {"Set-Cookie": self._cookie(result["session_token"], 28800)},
                )
                return

            session, user = self._require_session()
            self._check_csrf(session)

            if path == "/api/logout":
                self.app.logout(session.token, user["username"], self.client_address[0])
                self._json(
                    200,
                    {"ok": True},
                    {"Set-Cookie": self._cookie("", 0)},
                )
                return

            if path == "/api/users":
                payload = self._json_body()
                tenant_ids = payload.get("tenants") or []
                if not isinstance(tenant_ids, list):
                    raise ConsoleError(400, "tenants 必须是数组")
                result = self.app.create_user(
                    user,
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                    str(payload.get("role", "operator")),
                    [str(item) for item in tenant_ids],
                    str(payload.get("display_name", "")),
                    self.client_address[0],
                )
                self._json(200, {"ok": True, "user": result})
                return

            if path == "/api/me/password":
                payload = self._json_body()
                self.app.change_password(
                    user,
                    str(payload.get("old_password", "")),
                    str(payload.get("new_password", "")),
                    self.client_address[0],
                )
                self._json(
                    200,
                    {"ok": True, "relogin_required": True},
                    {"Set-Cookie": self._cookie("", 0)},
                )
                return

            password_match = re.fullmatch(r"/api/users/([a-z0-9_.-]{1,32})/password", path)
            if password_match:
                payload = self._json_body()
                self.app.reset_password(
                    user,
                    password_match.group(1),
                    str(payload.get("password", "")),
                    self.client_address[0],
                )
                self._json(200, {"ok": True})
                return

            # 按需扫描：起后台线程跑四步，立刻返回 running 状态，页面轮询进度。
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/scan", path)
            if match:
                payload = self._json_body()
                result = self.app.scan_start(
                    user,
                    match.group(1),
                    force=bool(payload.get("force")),
                    ip=self.client_address[0],
                )
                self._json(200, {"ok": True, **result})
                return

            # 委托台：一句话 → 意图 + 计划预览。只读，不调任何工具，不落库。
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/intake", path)
            if match:
                payload = self._json_body()
                text = payload.get("text", "")
                if not isinstance(text, str):
                    raise ConsoleError(400, "text 必须是字符串")
                result = self.app.intake_preview(user, match.group(1), text)
                self._json(200, {"ok": True, **result})
                return

            # 事实：显式写入，来源必须在白名单里。
            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/facts", path)
            if match:
                payload = self._json_body()
                result = self.app.facts_add(
                    user, match.group(1), payload, self.client_address[0]
                )
                self._json(200, {"ok": True, **result})
                return

            match = re.fullmatch(r"/api/tenants/([a-z0-9-]{1,40})/(approve|preview|publish)", path)
            if not match:
                self._error(404, "not found")
                return
            tenant_id, action = match.group(1), match.group(2)
            payload = self._json_body()

            if action == "approve":
                result = self.app.approve(
                    user,
                    tenant_id,
                    str(payload.get("product_id", "")),
                    str(payload.get("variant_key", "")),
                    self._int_field(payload, "ttl", 86400),
                    self.client_address[0],
                )
                self._json(200, {"ok": True, **result})
                return

            if action == "preview":
                result = self.app.preview(
                    user,
                    tenant_id,
                    str(payload.get("product_id", "")),
                    self._int_field(payload, "variant_index", 0),
                    self.client_address[0],
                )
                self._json(200, {"ok": True, **result})
                return

            result = self.app.publish(
                user,
                tenant_id,
                str(payload.get("product_id", "")),
                self._int_field(payload, "variant_index", 0),
                str(payload.get("approval_token", "")),
                self.client_address[0],
            )
            ok = bool(result.get("ok"))
            response = {"ok": ok, **result}
            if not ok:
                response["error"] = result.get("message", "发布失败")
            self._json(200 if ok else 409, response)
        except ConsoleError as exc:
            self._error(exc.status, exc.message)
        except Exception:
            traceback.print_exc()
            self._error(500, "服务器内部错误")

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            session, user = self._require_session()
            self._check_csrf(session)
            match = re.fullmatch(
                r"/api/tenants/([a-z0-9-]{1,40})/facts/([0-9A-Za-z][0-9A-Za-z._-]{0,63})", path
            )
            if not match:
                self._error(404, "not found")
                return
            query = parse_qs(urlparse(self.path).query)
            reason = (query.get("reason") or [""])[0]
            result = self.app.facts_revoke(
                user,
                match.group(1),
                match.group(2),
                reason=reason,
                ip=self.client_address[0],
            )
            self._json(200, {"ok": True, **result})
        except ConsoleError as exc:
            self._error(exc.status, exc.message)
        except Exception:
            traceback.print_exc()
            self._error(500, "服务器内部错误")

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        try:
            session, user = self._require_session()
            self._check_csrf(session)
            match = re.fullmatch(r"/api/users/([a-z0-9_.-]{1,32})", path)
            if not match:
                self._error(404, "not found")
                return
            payload = self._json_body()
            kwargs = {}
            if "role" in payload:
                kwargs["role"] = str(payload["role"])
            if "tenants" in payload:
                if not isinstance(payload["tenants"], list):
                    raise ConsoleError(400, "tenants 必须是数组")
                kwargs["tenant_ids"] = [str(item) for item in payload["tenants"]]
            if "disabled" in payload:
                if not isinstance(payload["disabled"], bool):
                    raise ConsoleError(400, "disabled 必须是布尔值")
                kwargs["disabled"] = payload["disabled"]
            if "display_name" in payload:
                kwargs["display_name"] = str(payload["display_name"])
            result = self.app.update_user(
                user,
                match.group(1),
                ip=self.client_address[0],
                **kwargs,
            )
            self._json(200, {"ok": True, "user": result})
        except ConsoleError as exc:
            self._error(exc.status, exc.message)
        except Exception:
            traceback.print_exc()
            self._error(500, "服务器内部错误")


class ConsoleServer:
    def __init__(
        self,
        app: ConsoleApp,
        host: str = "127.0.0.1",
        port: int = 8787,
        tls_cert: Path | str | None = None,
        tls_key: Path | str | None = None,
    ) -> None:
        if (tls_cert is None) != (tls_key is None):
            raise ValueError("tls_cert and tls_key must be provided together")
        handler = type("BoundConsoleHandler", (ConsoleHandler,), {"app": app})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.httpd.scheme = "http"
        if tls_cert:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(str(tls_cert), str(tls_key))
                self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
            except (OSError, ssl.SSLError):
                self.httpd.server_close()
                raise
            self.httpd.scheme = "https"
        self.host, self.port = self.httpd.server_address[:2]

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()

    def close(self) -> None:
        self.httpd.server_close()
