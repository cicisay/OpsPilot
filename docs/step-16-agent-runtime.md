# Step 16 · 主管改成带工具的 agent（D3 / D4 / D5）

Step 15 之前，`director` 是一次性调用：代码把 50 个 SKU 压成一份 digest（实测 32,689 字符，≈1.09 万 token）塞进 prompt，模型读完直接吐 JSON。问题不在 token，在**谁决定看什么**——选品依据是代码挑好的摘要，模型对某个商品有疑问时无法追问，只能凭摘要里剩下的那几行猜。

这一步把主管拆成两部分：代码给一份**浅目录**（够判断"要不要打开它"），模型用**七个只读工具**自己取深数据，每个结论必须引用真实发生过的观察。输出契约、配额、下游三件套（工坊 / 审查 / 发布）一行不改。

## 三层结构

| 层 | 文件 | 职责 |
|----|------|------|
| 可见性 | `runtime/gateway.py` | 按 `AgentPolicy` 过滤工具，只放 read 类；`attempts` 与 `executed` 分开计数 |
| 可追溯 | `runtime/ledger.py` | 每次调用编号 `obs-N`，内存留全量、落盘只留摘要与两个 sha1 |
| 预算 | `runtime/loop.py` | 步数 / token / 墙钟 / 工具调用 / submit 次数五重上限，超限即停 |

关键取舍：**证据不是模型自述，是账本里的编号**。模型写"该商品询盘率 0.8%"，代码只认它引用的 `obs-3` 是不是本次真调过、调的是不是允许当证据的工具、那次调用有没有成功。

## 七个只读工具

`build_research_registry()` 注册，schema 全部扁平（单层参数，字符数 231–589），不出现 `oneOf` / 嵌套对象——小模型对嵌套 schema 的合规率明显更差。

| 工具 | 参数 | 返回 | schema 字符 |
|------|------|------|------|
| `get_product` | `product_id` | 变体价格/成本/库存 + 当前刊登文案 | 231 |
| `get_metrics` | `product_id`, `days?` | 流量/询盘/订单/毛利/库存天数 + 类目中位数对比 | 293 |
| `get_benchmarks` | `category` | 类目中位数与样本量；样本 < 3 时 `trustworthy=false` | 242 |
| `get_reviews` | `product_id`, `limit?` | 评价原文，差评优先；无正文返回空数组 | 286 |
| `price_calc` | `product_id`, `price_cents` | 毛利率、成本底线、价格带 | 278 |
| `search_memory` | `query` | 记忆子串检索（经验/决策/实验） | 400 |
| `list_products` | `category?`, `flag?`, `sort?`, `limit?`, `offset?` | 分页目录：`total/returned/fields/rows` | 589 |

`list_products` 的排序只接受五个具名指标（`pv_asc` / `pv_desc` / `order_rate_asc` / `stock_days_desc` / `margin_asc`），没有"机会分"这种综合排序——一个没人能复现的排名就是穿了工具外衣的规则短名单。`limit` 夹到 1–20，返回体按 1,200 字符自限流，装不下就少给几行并带 `next_offset`，而不是让账本把一行 JSON 切成两半。

`build_registry()` 的 9 个旧工具一行未动，`EXPECTED_TOOLS` 仍按 9 个断言；研究工具走独立 registry，两套工具永不混在一个网关里。写类工具（`publish_listing` / `rollback` / `approve`）没有任何路径能进研究 agent 的可见列表，测试里对此有断言。

## 种子：一页目录，不是全量

一行 6 个字段（数组形式，靠 `catalogue_fields` 声明列序）：`id / title / category / pv / margin_pct / flags`。用具名对象放 20 行要 4,197 字符，直接超预算；改成数组 + 一次列名声明后是 1,833。

三处省 token 但不丢信息：

- 空 `flags` 写 `[]` 而不是省略（定长数组不能让列错位）；
- 标题截 20 字；
- `json.dumps(separators=(",", ":"))`。

种子只放**首屏 20 行，按 pv 升序**——打开第一页应该看到最可能出问题的商品，而不是随机抽样。同时给 `catalogue_size`、`categories`（`count/dead_stock/below_floor/median_pv`，最多 6 行，尾部折叠成 `+N more categories`）和 `catalogue_note`（"page 1 of 3 … an unseen row is not a healthy row"）。没有这一句，"没列出来"就会被读成"没问题"，而这正是要消灭的失败模式。

> 实测（globaltrade，50 件在售）：种子 **2,916 字符 ≈ 972 token**；500 SKU 时 2,817 字符；12 个类目时 1,820 字符。oneshot digest 32,689 字符 ≈ 10,896 token——种子是它的 8.9%。

`memory_digest` 也一起收紧了：去掉体检摘要（160 字）和第二、三条历史决策，只留 `health_score / weak_categories[:3] / active_products / last_selected_count / open_experiments / lessons / 最近 1 条决策`。要看全文可以调 `search_memory`，这部分的定位是"别重复上周"，不是"复述上周"。

## 证据规则

`evidence_errors()` 在代码里，不在 prompt 里：

1. 每个选品 ≥ 2 条 `evidence_refs`（`minimum` 参数，submit 用 2）；
2. 同一 `obs-N` 写两遍只算一条（`dict.fromkeys` 去重）；
3. 引用必须存在，否则 `unknown observation`；
4. `search_memory` 的结果是上下文不是证据（`EVIDENCE_TOOLS` 白名单排除）；
5. **失败的调用不算证据**——404 的 `get_product` 是"这个商品不存在"的事实，不是这个商品指标的证明；
6. **decline 与 submit 对称**：至少 1 条同规则的证据，且 `gateway.executed > 0`——一个商品都没打开，就不许说"本周不动"。这条比选品的门槛低（1 条 vs 2 条），因为判断更小，但不为零。


## 预算

```python
RESEARCH_BUDGET = Budget(
    max_steps=16, max_tool_calls=20, max_total_tokens=60_000,
    max_wall_seconds=240.0, max_submit_attempts=3,
)
RESEARCH_MAX_TOKENS = 8192   # 五次任务单带引用放不进 4096
```

`max_tool_calls` 只有一份：`Budget` → `gateway.set_budget()`，网关不自己存第二份计数。猜错工具名消耗 `attempts`，真正执行才记 `executed`，所以"模型乱猜"烧的是预算，不是数据。

## trace

`TraceRecorder.record()` 新增五个可选字段，旧调用不传则行格式不变：

```
{"run_id":"agent-1","step":1,"tool":"get_metrics","args_hash":"3837a815…","obs_id":"obs-1","tokens":10}
```

- 一次模型调用一行，终局那步也记（`obs_id` 为空）；
- `tokens` 是**本次调用**的 prompt + completion，累计值靠加总；预算判定不看它，看 `RunResult.tokens`；
- `args_hash` 只哈希参数，所以两个不同工具同参数会同哈希——靠 `tool` 字段区分；
- 业务 `run_id` 直接传给 `TraceRecorder`，trace 文件名与 `task_orders.json` 里的 `run_id` 同名，方便对账。

`scripts/inspect_trace.py` 按 run 打出工具序列与每步 `obs`；`scripts/make_large_snapshot.py` 用 `crc32` 派生扰动克隆出 N 件商品的压测快照。

## CLI

```powershell
python run.py director run                    # 默认 oneshot，行为与改造前一致
python run.py director run --runtime agent    # 带工具的研究 agent
python run.py agent run --runtime agent       # 主管段走 agent，工坊/合规不变
$env:OPS_RUNTIME = "agent"                    # 环境变量兜底，flag 优先
```

`provider_supports_tools()` 做能力门禁：`deepseek / qwen / glm / moonshot / minimax / openai` 走 allowlist；`ollama / local` 要显式 `LLM_TOOLS=1`。不支持的环境不会静默降级——带 `tools` 的请求不带 `response_format`，两者互斥。

## 与 oneshot 的差异

同一份 store、同一份 `normalize_sku`，逐键比对结果：

- 顶层：agent 比 oneshot 多一个 `inspected`（去重后的商品数），其余 12 个键完全一致（`generated_at / matched / model / notes / planner / quota / repair_issues / run_id / scanned / selected / store / store_health / violations`）；
- `inspected` 是"打开过几个商品"，`scanned` 仍是"在售几个"，两个数不重叠；
- 单个任务单只多 `evidence_refs` 一个键；
- `planner` 为 `"agent"`（oneshot 是 `"llm"`），`strategy.planner` 同步；
- `status` 只在内存里用于区分 decline / submit，落盘前 `pop` 掉；
- trace 多出 `step / tool / args_hash / obs_id / tokens` 五个字段。

`contracts.py` 一行未改：`normalize_task_order` 只在**传入** `evidence_refs` 时才透传，所以 oneshot 的 `task_orders.json` 键集与改造前逐字节一致（实测 13 键、无 `inspected`、单个任务单 30 键）。

## 越权与幻觉分开计数

```python
POLICY_LIMIT = 2   # tool_not_permitted → status="policy_violation"
MISUSE_LIMIT = 4   # unknown_tool       → status="tool_misuse"
POLICY_REMINDER_AT = 1
```

两件事以前共用一个计数器，于是一个把工具名记错的模型和一个正在试探越权的模型看起来一样。现在分开：**要一个没给你的工具**是安全信号，两次就停；**编一个根本不存在的工具名**是能力信号，给四次。`policy_violation` 只用于前者，后者报 `tool_misuse`，`ResearchError` 里带上 status 和一句区分性提示。

## 验证

- 单测：`Ran 476 tests · OK`（431 旧 + 20 网关/运行时 + 25 研究 agent）。
- 端到端（globaltrade 真实快照，50 件在售）：`scanned=50`，`selected=2`，`refs=['obs-1','obs-2'] / ['obs-3','obs-4']`，`violations=[]`。
- 下游兼容：agent 产出的 `task_orders.json` 直接喂 `workshop run --offline --planner rule` 与 `review run`，2 个方案包、6 个变体全通过，否决 0。
- 单轮体积（脚本化重放，4 次模型调用）：2,851 / 3,069 / 3,207 / 3,447 token，中位数 3,138；累计 12,574。比收窄种子前（5,272–5,780）降了约四成。
- 500 SKU 假运行：种子 20 行 / 2,817 字符，`scanned=500`、`inspected=1`，工具序列 `list_products → list_products → get_metrics → get_product → submit_task_orders`，选中的商品不在首屏 20 行内——发现闭环成立。

## 已知缺口与取舍

1. **实机验收没做成**。MiniMax 返回 `429 已达到 Token Plan 用量上限`，`--runtime agent` 的真实模型调用、真实 token usage、真实工具序列对比都只能用脚本化 client 重放。上面所有数字测量的是**真实消息体积**，不是供应商返回的 usage；接上可用额度后需要重跑一遍才算数。
2. **"两次同快照工具序列不同"证明的是运行时没有写死顺序**，不是证明模型会变。脚本 client 下序列差异是构造出来的；真正确认随机性要靠实机。
3. **3k 预算是硬的，行格式因此变成了数组**。20 行具名对象要 4,197 字符，怎么截字段都放不进 3,000；改成数组 + `catalogue_fields` 后是 1,833。代价是模型要读一次列名映射——用列错位的风险换确定性预算。
4. **`quota.guarded` 语义与 oneshot 不同**。oneshot 是"超上限被裁剪"才置真，agent 是"模型主动少选、低于下限"也置真，并写明"未抬到下限"。同一个字段两种含义，已在 `reason` 里分开措辞。
5. **`price_calc` 与旧工具的价格带口径不同**。旧 `supervisor` 印出的 `band_low > band_high`（40% 毛利对应更高售价，两处公式写反），新工具修正为 `low = cost/(1-0.25)`、`high = cost/(1-0.40)`；旧代码未动，避免影响既有测试与产物。
6. **`inspected` 一个名字两种口径**。`task_orders.json` 里是"去重商品数"（`len(inspected_product_ids)`），`decisions.jsonl` 里是"工具调用次数"（`gateway.executed`）。两者同名不同义，要统一得先定哪个才算"检查深度"。
7. **`catalogue_note` 只在分页时出现**。20 件以内没有 note，因为那时它确实就是全量；写死一句"这是分页"会让模型去翻不存在的下一页。
