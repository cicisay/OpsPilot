# 在 GlobalTrade 外贸站上测试 OpsPilot：Runbook

> 目标：验证两件事——**前端是否真的更好用、更像同一个品牌**；**系统是否存在可被利用的漏洞**。
>
> 原则：真实店铺只读优先；写入测试只用 QA 商品/QA 数据库；任何发布都必须有令牌和回滚方案。

---

## 0. 先分清三个“测试对象”

| 对象 | 地址/位置 | 测什么 |
|---|---|---|
| OpsPilot 运营台 | `http://127.0.0.1:8787`（真实）/ `8788`（离线 Demo） | 登录、租户、方案、审批、预览、发布、用户、审计、前端视觉 |
| 外贸站连接器 | `http://localhost:3000/api/agent/*` | 鉴权、dry-run、幂等、版本、回滚、CORS、限流 |
| 外贸站前台 | `http://localhost:3000` | 外贸站前台页面的视觉/交互；OpsPilot 不应改它 |

OpsPilot 前端改造只允许改 `console/static`，**不能改外贸站**。

---

## 1. 环境隔离（最重要）

### 1.1 备份数据库

```powershell
$web = $env:SITE_WEB_DIR
Copy-Item "$web\prisma\dev.db" "$web\prisma\dev.db.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

写入测试结束后恢复：

```powershell
# 先停站点进程，再恢复
Copy-Item "$web\prisma\dev.db.bak-..." "$web\prisma\dev.db" -Force
```

### 1.2 更推荐：QA 数据库 + 第二端口

```powershell
$web = $env:SITE_WEB_DIR
Copy-Item "$web\prisma\dev.db" "$web\prisma\qa.db"
$env:DATABASE_URL = "file:./qa.db"   # 先确认 schema 的相对路径
cd $web
pnpm dev --port 3001
```

用 `http://localhost:3001` 做写入测试，QA 商品只在这个库；不碰 3000 的真实数据。

> 如果 Prisma 的 `DATABASE_URL` 相对路径不是 `prisma/`，按 `schema.prisma` 的实际位置调整；启动后先 `site check` 确认读的是 QA 库。

### 1.3 QA 商品

- 在 QA 库建一个 `QA-Agent-Test` 商品 + 1 个变体；
- 记录 `productId`、`variantId`、当前价格/库存/版本；
- 所有发布/回滚只对这个商品。

---

## 2. 前端视觉与可用性测试

### 2.1 启动

```powershell
# 离线 Demo：不依赖站点，最稳
python run.py console serve --host 127.0.0.1 --port 8788 --demo

# 真实联调
$env:SITE_AGENT_TOKEN = "<站点 token>"
python run.py console serve --host 127.0.0.1 --port 8787
```

### 2.2 三档截图

- 1440×900（桌面）
- 1024×768（小桌面/平板）
- 390×844（手机）

检查：

- 无整页横向滚动；表格可滚动；
- 首屏能看到任务/方案，不是被大 hero 占满；
- 字体、主色、圆角、阴影与外贸站同品牌；
- 空状态、加载、错误都有文案；
- 键盘 Tab 可走完登录 → 选方案 → 预览；
- 控制台无 JS 报错、无 404、无外部请求。

工具：浏览器 DevTools、Lighthouse（Accessibility/Performance）、axe DevTools。

### 2.3 交互回归（必须全过）

| 流程 | 操作 | 期望 |
|---|---|---|
| 登录 | 正确/错误密码 | 200 / 401；连续 6 次错误 429 |
| 租户切换 | globaltrade ↔ northwind-demo | 数据切换；operator 不能看未授权租户 |
| 总览 | 打开 | 30 商品、5 任务单、3 方案 |
| 方案 | 点方案 | before/after、合规、价格正确 |
| 预览 | 点“预览服务端 diff” | 200；改价/字段 diff |
| 审批 | approver 签发 | 返回一次性令牌 |
| 发布 | 无令牌/无效令牌 | 400 / 409；0 写入 |
| Demo 发布 | `--demo` 点发布 | 400“演示模式只读” |
| 用户管理 | 创建/停用/改角色 | 成功；停用后登录 401 |
| 审计 | 查询/导出 CSV | 200；含 login/approve/publish |
| 改密 | 旧密码/新密码 | 成功后强制重新登录 |

### 2.4 前端安全静态检查

```powershell
cd <项目根目录>
node --check src/ops_agent/console/static/app.js

# 不应有 inline script/style 或外链
Select-String -Path src/ops_agent/console/static/*.html,src/ops_agent/console/static/*.js `
  -Pattern "<script>|onclick=|https?://|localStorage|sessionStorage"
```

允许的 `http://` 只有文档/注释；生产静态资源应全部同源。

---

## 3. 自动化冒烟

### 3.1 全量单元测试

```powershell
python -m unittest discover -s tests -q
```

当前基线：**972 passed**。

### 3.2 运营台安全冒烟

```powershell
python scripts/security_smoke.py `
  --base-url http://127.0.0.1:8788 `
  --username admin --password "..." `
  --operator-user operator --operator-password "..."
```

覆盖：

- 未登录 401
- 登录成功
- CSRF 缺失 403
- 租户列表/总览
- 路径穿越 404
- 审计查询/CSV
- 错误密码 401
- operator 列用户 403、签发令牌 403

当前基线：**13/13 passed**。

---

## 4. 连接器/API 安全测试（对外贸站）

### 4.1 鉴权

```powershell
$env:SITE_AGENT_TOKEN = "wrong-token-12345678"
python run.py site check          # 期望 401，无 traceback
```

### 4.2 CORS

```powershell
Invoke-WebRequest -Uri "http://localhost:3000/api/agent/health" `
  -Headers @{ Origin = "http://evil.example" } -UseBasicParsing
```

期望：响应里**没有** `Access-Control-Allow-Origin`。连接器是服务端调用，不应该开 CORS。

### 4.3 dry-run 不落库

1. 记录 QA 商品版本数；
2. 调 `patch_product?dryRun=true` / `patch_variant?dryRun=true`；
3. 再查版本数，期望不变；
4. 数据库 `AgentListingVersion` 不新增。

### 4.4 幂等重放

对 QA 商品用同一个 `Idempotency-Key` 连续发两次 `PATCH`：

- 期望第二次返回第一次的 `versionId`；
- 版本数只 +1；
- 内容只改一次。

### 4.5 并发同 key（已知风险）

两个线程同时用同一个 `Idempotency-Key` 发请求：

- 期望只产生一个版本；
- 如果产生两个版本/两次写入，记录为 **P0**：站点侧“先执行后占位”竞态，需要“先占位再执行”。

### 4.6 审批令牌

- 无令牌发布 → 400；
- 无效令牌 → 409 + `ok:false`；
- 同一令牌用两次 → 第二次失败；
- 令牌给 A 商品，拿去发 B → 失败；
- 令牌过期 → 失败；
- Demo 模式发布 → 400。

### 4.7 注入与越权

| 测试 | 输入 | 期望 |
|---|---|---|
| XSS | QA 商品标题 `<img src=x onerror=alert(1)>` | OpsPilot 页面显示为文本，不执行；站点前台另测 |
| SQLi | 登录名 `' OR 1=1--` | 401，无 500 |
| 路径穿越 | `/api/tenants/../overview` | 404 |
| 跨租户 | operator 看 northwind-demo | 403 |
| 越权 | operator 列用户/签发令牌 | 403 |
| 限流 | 连续 6 次错误登录 | 429 |
| Cookie | 登录响应 | `HttpOnly; SameSite=Strict; Path=/`；HTTPS 下应 `Secure` |
| 密钥泄漏 | DOM/localStorage/网络 | 无站点 token、无审批令牌 |

### 4.8 发布/回滚（仅 QA 库/QA 商品）

1. 备份 QA 库；
2. 对 QA 商品签发令牌；
3. 发布一版；
4. 查版本/审计，确认版本 +1；
5. 回滚；
6. 查价格/标题恢复；
7. 恢复 QA 库或删除 QA 商品。

---

## 5. “有没有效果”怎么测

### 5.1 前端效果（OpsPilot 本身）

找 5 个同事做同一组任务：

- 找到本周 5 个 TaskOrder；
- 选一个方案并预览；
- 签发令牌；
- 完成一次发布（Demo/QA）；
- 查审计。

记录：

- 任务完成时间；
- 误点/回退次数；
- 是否用错角色/租户；
- SUS 问卷（0–100）。

改造前后各跑一轮，比较时间、错误率、SUS。目标是“更快、更少误操作”，不是“更花哨”。

### 5.2 业务效果（外贸站）

选 10–20 个处理 SKU + 10–20 个匹配的对照 SKU：

- 同品类、同价格带、同流量量级；
- 处理组按 OpsPilot 方案改，组不做；
- 观察 4–6 周（B2B 流量低，时间短看不出）；
- 主指标：询盘率 = 询盘/PV；
- 次指标：下单率 = 订单/PV、毛利、退货/差评；
- 用双重差分（DiD）：`(处理后-处理前) - (对照后-对照前)`；
- 成功标准先写死，例如“询盘率 +15%，毛利下降 ≤2pt”；
- 不承诺 GMV；没有显著结果就如实说“样本不足/方向不成立”。

### 5.3 Agent 过程指标

- TaskOrder 命中率（人工复核）；
- 方案合规通过率；
- 发布成功率；
- 回滚率；
- 平均审批时间。

---

## 6. 证据与退出标准

每次测试留：

- 截图（三档）；
- HTTP 状态码/响应；
- 测试输出（972 tests、13/13 smoke）；
- 版本 ID、审计 CSV、发布 diff；
- 数据库备份路径；
- 失败项清单（P0/P1/P2）。

退出标准：

- P0 = 0：鉴权绕过、跨租户、CSRF、令牌泄漏、无令牌发布、XSS 执行。
- P1 = 0：核心流程不可用、控制台报错、移动端不可操作。
- P2 可记录：文案、间距、非关键视觉。

---

## 7. 已知边界（不要误判为“新漏洞”）

- 站点侧幂等并发竞态：客户端锁只保护本进程；
- TLS 未做真实握手（本机无 openssl）；
- Shopify/Woo 只有接口，没有 OAuth/映射；
- JSON 与 SQLite 双存储，切 DB 后 JSON 不再同步；
- 审计导出最多 5000 行；
- 没有真实 A/B，不承诺 GMV。
