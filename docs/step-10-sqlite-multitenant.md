# Step 8 — SQLite 多租户、审计导出与连接器接口

## 目标

把用户/租户从 JSON 搬到 SQLite，按 tenant_id 做成员关系；补审计 CSV 导出；把平台连接器收口成明确接口，未实现的平台必须报错，不能假装能用。

## 交付物

| 文件 | 作用 |
|---|---|
| `console/db.py` | SQLite users / user_tenants / tenants；JSON 迁移 |
| `console/connectors.py` | 连接器注册；selfhosted 可用，Shopify/Woo 明确报错 |
| `console/app.py` | 审计 CSV 导出、动作审计 |
| `console/server.py` | `/api/audit/export` |
| `cli.py` | `console migrate`、`console serve --db` |
| `tests/test_console_db.py` | SQLite 用户/租户/连接器测试 |

## 数据库结构

```sql
users(username PK, display_name, role, password_hash, disabled)
user_tenants(username, tenant_id, PK(username, tenant_id))
tenants(tenant_id PK, name, site_base_url, token_env,
        data_dir, currency, timezone, platform)
```

- `platform` 默认 `selfhosted`。
- 用户与租户是多对多；`for_user` 按成员关系过滤。
- 每个租户的数据目录仍是 `data/tenants/{id}/`，DB 只存元数据。
- 审计仍在 `data/console/audit.db`（独立 SQLite）。

## 迁移与运行

```powershell
# 新初始化：同时写 JSON 和 SQLite
python run.py console init --admin-password "..." --approver-password "..." --operator-password "..."

# 已有 JSON：单独迁移
python run.py console migrate

# 启动：存在 console.db 时默认走 SQLite
python run.py console serve --host 127.0.0.1 --port 8787

# 指定 DB
python run.py console serve --db data/console/console.db
```

迁移规则：

- 用户不存在才插入，避免覆盖 DB 中已改的密码；
- 租户 `INSERT OR REPLACE`；
- 密码哈希原样迁移，不重新哈希。

## 审计导出

- 页面：管理面板 → 审计日志 → “导出 CSV”。
- API：`GET /api/audit/export?tenant=&limit=5000`。
- CSV 带 UTF-8 BOM，Excel 直接打开不乱码。
- 防 CSV 公式注入：字段以 `= + - @` 开头时前置单引号。
- 非管理员必须指定自己的租户。

## 连接器

`build_site(tenant)`：

- `selfhosted` / `custom`：用 `SITE_AGENT_TOKEN` + Agent API，已实现。
- `shopify` / `woocommerce`：抛 `ConnectorError`，明确提示尚未实现。
- 未配置站点/token：明确报错。

这一步只做“接口和边界”，没有伪造 Shopify/Woo 的 OAuth、商品映射和库存同步。

## 验收

- 134 个单元测试通过。
- SQLite 端到端：
  - `console init` 创建 `console.db`；
  - 3 个用户、2 个租户、成员关系正确；
  - 登录/总览/预览 200；
  - 审计导出 200，`text/csv`，包含 login 记录；
  - 非管理员跨租户 403。
- JSON 迁移命令可重复执行；已存在用户不覆盖。
- 真实店铺只做 dry-run，0 写入。

## Bug / 边界修正

1. **CSV 公式注入**：审计字段可被用户输入影响，导出时对 `= + - @` 开头加 `'`，避免 Excel 执行公式。
2. **旧 DB 没有 platform 列**：启动时检查 `PRAGMA table_info(tenants)`，缺失则 `ALTER TABLE` 补列。
3. **迁移缺 data_dir**：用 `tenants/{id}` 兜底，不再 KeyError。
4. **JSON/DB 双写漂移**：切到 DB 后，用户管理只写 DB；JSON 只作为迁移源。已在文档写明，避免有人改 JSON 以为生效。

## 已知限制

1. Shopify / WooCommerce 连接器尚未实现，只有接口和错误边界。
2. SQLite 单文件；多进程并发写要加 WAL/锁，生产建议 Postgres。
3. `user_tenants` 没有外键约束到 tenants；应用层校验，DB 层未强制。
4. JSON 与 DB 并存，旧 JSON 不再自动同步；迁移是单向的。
5. 审计导出最多 5000 行，没有分页/异步导出。
6. TLS 仍未做真实握手（本机无 openssl）。
7. 站点侧幂等并发竞态仍在。

## 下一步

1. Shopify / WooCommerce 真连接器：OAuth、商品映射、库存/价格同步。
2. Postgres + 行级权限 + 审计保留策略。
3. 导出分页/异步任务。
4. LLM planner。
