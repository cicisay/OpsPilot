# Step 7 — P0 安全加固

## 目标

把审计里“上线前必须做”的 P0 落到代码：真实审批、发布锁、按店铺隔离、目标变体、分市场合规、token 轮换。

## 交付物

| 文件 | 作用 |
|---|---|
| `src/ops_agent/approval_store.py` | 持久化一次性审批令牌（跨会话） |
| `src/ops_agent/publish.py` | 令牌校验、商品锁、店铺级幂等键、目标变体、补偿回滚 |
| `src/ops_agent/compliance.py` | US/DE/UK 分市场词库 |
| `src/ops_agent/site_platform.py` | token provider / `SITE_AGENT_TOKEN_FILE` |
| `src/ops_agent/cli.py` | `approve issue/list`、`publish --approval-token`、`--market` |
| `tests/test_approval_store.py` | 5 个令牌测试 |

## 审批流程

```powershell
# 1) 审批人先设独立密钥（不是站点 token）
$env:OPS_APPROVER_TOKEN = "..."

# 2) 签发一次性令牌
python run.py approve issue `
  --product-id <productId> `
  --variant-key <variantKey> `
  --operator alice

# 3) 执行人拿令牌发布
python run.py publish run --limit 1 --approval-token APR-...
```

规则：

- 令牌绑定 `productId + variantKey + action`；
- 默认 TTL 24 小时；
- 一次即废；
- 没有令牌 → 只 dry-run；
- 令牌无效/过期/已用/对象不符 → `invalid_approval`，不写入；
- 合规未通过 → `blocked`，不消耗令牌；
- 同一商品正在发布 → `busy`，不消耗令牌。

## 其他 P0 改动

1. **商品锁**：`data/locks/{productId}.lock`，防止同一商品并发发布；600 秒 stale 自动清理。
2. **店铺级幂等键**：`pub-{storeSlug}-{productId}-{variantKey}-p/-v`，避免多店铺串 key。
3. **目标变体**：工坊方案写入 `target_variant_id`；发布按目标变体，不再默认最低价 SKU。
4. **分市场合规**：`--market US|DE|UK`；DE 拦截 `klimaneutral` / `tüv-geprüft` / `garantiert`，UK 拦截 `risk-free` / `No.1`。
5. **token 轮换**：`SitePlatform` 每次请求重新取 token；设置 `SITE_AGENT_TOKEN_FILE` 后，改文件即可轮换，无需重启。

## 验收

- 106 个测试通过。
- `approve issue`：无审批密钥时报错；有密钥时签发成功。
- `approve list`：只显示未使用令牌，且脱敏。
- `publish --approval-token INVALID`：`令牌无效`，0 写入。
- `publish` 不带令牌：只 dry-run，0 写入。
- 有效令牌未对真实站点执行发布；真实写入路径由假站点单测覆盖。
- 市场词库：DE/UK 测试通过，US 不受影响。

## 仍然存在的站点侧问题

1. 站点幂等键仍是“先执行后占位”，并发同 key 可能双写；我们的商品锁只保护本 Agent。
2. 站点 token 没有过期/轮换接口；`SITE_AGENT_TOKEN_FILE` 只是客户端读取方式。
3. 还没有运营台真实登录；`OPS_APPROVER_TOKEN` 是 CLI 级审批密钥，生产要换成账号 + 角色 + 审批 UI。
4. 多租户全链路 `storeId` 隔离还没做；当前幂等键和快照已按店铺命名，但数据库/API 还没有租户字段。

## 下一步

Step 6：OpsPilot 前端 + 真实登录/审批 + 多租户。
