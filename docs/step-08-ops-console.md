# Step 6 — OpsPilot 运营台 + 真实登录/审批 + 多租户

## 目标

把之前只有 CLI 的四角色流水线，做成一个独立运营台：登录、看本周动作、看方案 before/after、签发审批令牌、预览 diff、发布、看记录；同时按租户隔离数据。

## 交付物

| 文件 | 作用 |
|---|---|
| `src/ops_agent/console/auth.py` | PBKDF2 密码、会话、CSRF、登录限流 |
| `src/ops_agent/console/tenants.py` | 租户注册表、数据目录、访问控制 |
| `src/ops_agent/console/app.py` | 登录、总览、审批、预览、发布服务 |
| `src/ops_agent/console/server.py` | stdlib HTTP 服务、路由、安全头 |
| `src/ops_agent/console/static/` | 登录页 + 运营台前端 |
| `src/ops_agent/cli.py` | `console init` / `console serve` |
| `tests/test_console.py` | 10 个认证、HTTP、角色、租户测试 |

## 怎么用

```powershell
# 1) 初始化（会创建 admin / approver / operator 和两个租户）
python run.py console init `
  --admin-password "..." `
  --approver-password "..." `
  --operator-password "..."

# 2) 启动
$env:SITE_AGENT_TOKEN = "<站点 token>"
python run.py console serve --host 127.0.0.1 --port 8787
```

打开 `http://127.0.0.1:8787`：

1. 登录；
2. 选店铺；
3. 看本周动作、方案；
4. 审批人点“签发审批令牌”；
5. 运营粘贴令牌，先“预览服务端 diff”，再“发布”；
6. 发布记录里看结果。

## 安全设计

| 项 | 实现 |
|---|---|
| 密码 | PBKDF2-SHA256，20 万次迭代，随机 salt |
| 会话 | 随机 token，内存存储，8 小时过期 |
| Cookie | HttpOnly + SameSite=Strict + Path=/ |
| CSRF | 所有写操作要求 `X-CSRF-Token` |
| 登录限流 | 同 IP + 用户名 5 次失败锁 5 分钟 |
| 权限 | admin / approver / operator 三角色 |
| 租户 | admin 全部；approver / operator 仅授权店铺 |
| 响应头 | CSP、nosniff、DENY frame、no-referrer |
| 输入 | JSON Content-Type 校验、64KB 请求体上限 |
| XSS | 前端统一 `esc()`；CSP 禁止内联脚本 |
| 发布 | 必须有效审批令牌；失败返回 409 |

## 多租户

- 每个租户独立 `data/tenants/{tenant_id}/`：任务单、方案、审查、发布记录、审批令牌、快照、锁。
- `tenants.json` 只存站点地址和 token 的环境变量名，不存 token 本身。
- `northwind-demo` 是第二租户，用于演示切换；只有 admin 能看。
- 这是“目录级隔离”，不是数据库级多租户；生产要接租户字段和行级权限。

## 验收

- 117 个单元测试通过。
- HTTP 端到端：
  - 登录 200，`/api/me` 200，租户列表 2 个；
  - 总览 5 个任务单 / 3 个方案；
  - 审批 200，返回 `APR-...`；
  - 预览 200，改价 10.0%，合规通过；
  - 无效令牌发布 409、`ok:false`、0 写入；
  - 缺 CSRF 403；
  - operator 签发令牌 403；
  - operator / approver 跨租户 403；
  - 路径穿越租户 404；
  - CSP 头存在。
- 真实站点只做 dry-run，未写入。

## Bug 记录

**Bug 1：初始化把两个租户都授权给了所有用户。**
`console init` 给 approver / operator 也写了 `["globaltrade", "northwind-demo"]`，operator 能看第二租户。修复：admin 两个租户，approver / operator 只给 globaltrade；重新初始化后跨租户 403。

**Bug 2：发布失败仍返回 HTTP 200 + `ok:true`。**
无效令牌时 `ConsoleApp.publish` 返回 `ok:false`，但 HTTP 层固定包了 `{"ok": True}`，前端会误判成功。修复：HTTP 层按结果返回 200/409，并带 `error`；前端会显示失败原因。

## 已知限制

1. 演示密码是初始化时手输的，首次登录后应改；目前没有改密 UI。
2. 会话在内存里，重启运营台会要求重新登录。
3. 本地 HTTP，没有 HTTPS；生产要放反向代理 + TLS。
4. 没有登录/审批操作审计表；只有 approvals 和 publish_results 文件。
5. 租户隔离是目录级；生产要多租户数据库与行级权限。
6. `northwind-demo` 的数据是 globaltrade 的副本，仅用于演示切换。
7. 站点侧幂等并发竞态仍在，运营台的商品锁只保护本进程。

## 下一步

1. 用户管理 UI（改密、停用、角色调整）与审计日志。
2. HTTPS / 反向代理 / 会话持久化。
3. 数据库级多租户与平台连接器（Shopify / WooCommerce）。
4. LLM planner（保持 TaskOrder / 方案结构不变）。
