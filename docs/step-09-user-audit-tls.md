# Step 7 — 用户管理、审计日志与 HTTPS

## 目标

运营台不能只有登录，还要能管人、能追责、能走 HTTPS。这一步补三件事：

1. 管理员可以创建/改角色/改租户/停用/重置密码；用户可以改自己的密码。
2. 登录、审批、预览、发布、用户变更写入 SQLite 审计表，按租户可查。
3. 服务支持 `--tls-cert/--tls-key`，可跑 HTTPS。

## 交付物

| 文件 | 作用 |
|---|---|
| `console/auth.py` | `disabled`、改角色/租户、重置密码、最后一个管理员保护 |
| `console/audit.py` | SQLite 审计表，按租户/动作查询 |
| `console/app.py` | 用户管理、审计查询、动作写审计、会话失效 |
| `console/server.py` | `/api/users`、`/api/audit`、`PATCH`、TLS |
| `console/static/` | 用户管理面板、审计表、改密弹窗 |
| `tests/test_console.py` | 用户/审计/禁用/TLS 配置测试 |

## 用户管理

- 管理员：`GET /api/users`、`POST /api/users`、`PATCH /api/users/{name}`、`POST /api/users/{name}/password`。
- 自己改密：`POST /api/me/password`，必须带原密码。
- 停用用户会立即销毁该用户所有会话；密码重置同样。
- 不能停用或降级最后一个启用的 admin。
- 密码最短 8 位，PBKDF2-SHA256 存储。

## 审计日志

SQLite 表 `audit`：

| 字段 | 含义 |
|---|---|
| ts | UTC 时间 |
| actor | 操作人 |
| action | login / logout / approve / preview / publish / user.* / password.change |
| tenant | 租户 |
| target | 商品/用户/令牌对象（不含 token 本身） |
| ip | 来源 IP |
| status | ok / failed / blocked / invalid_approval / published |
| detail | 简短说明，不写密码或令牌 |

- 管理员可查全部；非管理员必须指定自己的租户。
- 登录失败、限流、角色拒绝都会留下记录。
- 数据库文件：`data/console/audit.db`。

## HTTPS

```powershell
python run.py console serve `
  --host 127.0.0.1 --port 8787 `
  --tls-cert "C:\path\cert.pem" `
  --tls-key "C:\path\key.pem"
```

- TLS 1.2+；HTTPS 下自动加 HSTS。
- 本机没有 openssl，未生成/握手真实证书；生产建议用 Nginx/Caddy 反向代理 + 正式证书。
- 只给 cert 或只给 key 会直接报错，不会启动半配置服务。

## 验收

- 124 个单元测试通过。
- HTTP 端到端：
  - admin 登录、列用户、创建 `qa-bot`、停用；
  - `qa-bot` 登录后能看 globaltrade 总览；
  - 停用后登录 401；
  - 审计里有 `login` / `user.create` / `user.update`；
  - operator 列用户 403、跨租户 403；
  - 缺 CSRF 403。
- 测试用户、测试审批令牌已清理；审计库已重置为空。

## Bug / 安全修正

1. **停用用户仍可登录（设计缺口）**：新增 `disabled` 后，`authenticate` 必须拒绝；`session_user` 也要让已停用用户的会话失效。
2. **TLS 半配置泄漏端口**：只在 `tls_cert` 或只在 `tls_key` 时，原实现会先绑定端口再报错。改为先校验，再绑定。
3. **用户变更会话不失效**：改角色/改租户/停用/重置密码后，旧会话仍可能保留权限。现在统一 `destroy_user`。
4. **审计泄露敏感值**：审计只记对象和状态，不写密码、审批令牌明文。

## 已知限制

1. 用户和租户仍是 JSON 文件；审计是 SQLite。完整“数据库级多租户”还没做。
2. 没有删除用户、强制下线、登录设备列表。
3. 审计库是本地单文件；生产要接集中日志/保留策略。
4. TLS 没有握手实测（本机无 openssl）；建议反向代理。
5. Shopify / WooCommerce 连接器尚未实现。
6. 站点侧幂等并发竞态仍在。

## 下一步

1. 用户/租户迁移到 SQLite/Postgres，按 tenant_id 行级隔离。
2. Shopify / WooCommerce 连接器（OAuth、商品映射、库存同步）。
3. 审计导出、保留策略、告警。
4. LLM planner。
