# Step 2.5 — 真实店铺适配器（SitePlatform）

## 本步目标

把 Agent 从“50 个合成 SKU 的沙盘”接到真实店铺。真实店铺已经有隐藏连接器 `/api/agent/*`（v1.1），这一步不碰站点，只写一个 Python 适配器：

- 读店铺、商品、指标、版本、变更流；
- 支持 dry-run 预演；
- 支持幂等写入与回滚；
- 测试不依赖真实站点。

## 交付物

| 文件 | 作用 |
|---|---|
| `src/ops_agent/site_platform.py` | 真实店铺适配器：HTTP、dataclass、错误映射、分页 |
| `src/ops_agent/cli.py` | 新增 `site check / products / sync / dry-run` |
| `tests/test_site_platform.py` | 9 个适配器测试，用假 HTTP 传输层 |
| `data/site_snapshot.json` | 真实店铺 30 个商品的本地快照 |

## 关键决策

1. **只走 HTTP，不 import 站点代码、不直连 SQLite。**
   连接器是唯一契约；换一个站点只换 base URL 和 token。
2. **不为真实数据硬套 `SKU` 模型。**
   合成 `SKU` 有 image_score / title_score / CTR，真实商品没有；新开 `SiteStore / SiteProduct / SiteVariant / SiteMetrics`，字段和 API 一一对应。
3. **指标口径跟着站点走。**
   B2B 站没有“曝光 / 点击”，只有 PV、询盘、订单、评价；所以用 `inquiry_rate = inquiries / pv`、`order_rate = orders / pv`，不再假装有 CTR。
4. **Token 只从环境变量读。**
   `SITE_AGENT_TOKEN`，不落代码、不落快照；快照里只有店铺和商品数据。
5. **测试注入传输层。**
   `SitePlatform(transport=...)` 接收假 HTTP，CI 不需要启动站点。

## 难点与解决

**难点 1：真实商品和合成 SKU 的字段对不上。**
合成数据有“主图评分、标题评分、退货率”，真实商品只有价格、成本、库存、PV、询盘、订单、评价。硬套会让一半字段变成假值。解决：新建站点专用模型，只保留真实存在的字段；诊断逻辑后面按真实指标重写。

**难点 2：没有 CTR，怎么判断“点击低”。**
站点是 B2B，PV 就是商品页访问，没有曝光。诊断要从“点击率”改成“访问→询盘率”和“访问→下单率”。这不是换名字，是换因果：PV 高、询盘低，说明详情页/价格/信任不足；PV 低，说明流量或关键词问题。

**难点 3：token 泄露风险。**
token 一旦进了代码、日志、快照，就等于把店铺后门交出去。解决：只从 `SITE_AGENT_TOKEN` 读；CLI 不打印 token；同步快照只写 store/health/products；测试里用假 token。

**难点 4：测试不能依赖真实站点。**
站点要启动、要 token、要数据，CI 里不可靠。解决：适配器把 HTTP 调用抽成 `transport`，测试注入假响应，覆盖 401、分页、dry-run、幂等头、回滚 body。

**难点 5：错误要可判断，而不是一句异常。**
真实 API 返回 `{message, code}`，Agent 要根据 code 决定重试、换参数还是升级人工。解决：统一 `SiteAPIError(status, code, message)`，调用方按 `code` 分支。

## 验收结果

- `python -m unittest discover -s tests -q`：51 个测试通过（新增 9 个）。
- `python run.py site check`：读到店铺 `GlobalTrade Manufacturing`、API 1.1.0、30 个商品 / 46 个 SKU、`dbWritable=True`、7 项 capabilities。
- `python run.py site products --limit 5`：输出真实 PV、询盘、订单、毛利率、库存。
- `python run.py site sync`：30 个商品写入 `data/site_snapshot.json`，无 token。
- `python run.py site dry-run --variant-id ... --price-cents 1180`：返回 diff，未落库。

## 设计问答

**Q1：为什么适配器不直接读数据库？**
A：直连数据库会绕过站点的校验、事务和审计，换一个站点就不可移植。HTTP 连接器是契约，站点内部怎么实现不关 Agent 的事；也方便以后接 Shopify / WooCommerce。

**Q2：为什么新建 SiteProduct，不复用原来的 SKU？**
A：原来的 SKU 是给模拟器用的，带 image_score、CTR 这些真实站点没有的字段。硬套会让模型里一半是假值，诊断会算错。宁可多一层模型，也不让假字段污染真实决策。

**Q3：真实站点没有 CTR，你怎么诊断？**
A：换成 B2B 的两个率：`inquiry_rate = 询盘 / PV`、`order_rate = 订单 / PV`。PV 高、询盘低，先查详情页、价格、信任背书；PV 低，先查关键词和类目。后面 Step 3 的诊断表会按这套口径重写。

**Q4：测试怎么不依赖真实站点？**
A：`SitePlatform` 的 HTTP 调用是可注入的 `transport`。测试里塞假响应，覆盖 401、分页、dry-run 查询参数、幂等头、回滚 body；真实联调用 `site check` 手动跑。

**Q5：token 怎么保证不泄露？**
A：只从 `SITE_AGENT_TOKEN` 环境变量读；CLI 不打印；快照只写 store/health/products；测试用假 token。万一要贴日志，先脱敏。

**Q6：dry-run 和幂等在适配器层怎么用？**
A：`patch_variant(..., dry_run=True)` 只发 `?dryRun=true`，返回 diff，不写库；`idempotency_key` 原样放进 `Idempotency-Key` 头，网络重试不会重复改价。两者都在适配器里，不靠调用方拼 URL。

## 已知限制

1. 真实站点指标没有“曝光 / CTR”，Step 3 的诊断口径要按 PV/询盘/订单重写。
2. 当前适配器只支持单店铺；多店铺需要按 storeId 隔离 token 和快照。
3. 幂等键在站点侧是全局唯一，没有按 storeId 隔离；多租户前要改。
4. 适配器不缓存；每次 `site check` 都会请求站点。快照只用于离线分析。

## 下一步

Step 3：运营主管 Agent。用 `SitePlatform` 读真实店铺，做体检、选品、诊断，输出 TaskOrder；先把规则 + 真实指标跑通，再接 LLM。
