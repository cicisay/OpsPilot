# Step 2 — 工具层：9 个工具、mock 平台、审批与合规

## 本步目标

让三个 Agent 有“手”：能查数据、能生成草稿、能过合规、能发布、能回滚；并且“能不能做”由工具边界决定，不靠提示词自觉。

## 交付物

| 文件 | 作用 |
|---|---|
| `src/ops_agent/platform.py` | mock 平台：店铺快照、Listing 版本、发布、回滚 |
| `src/ops_agent/approvals.py` | 一次性审批令牌：绑定 SKU/动作、会过期 |
| `src/ops_agent/compliance.py` | 规则化合规检查：绝对化、品牌词、版权、禁售 |
| `src/ops_agent/tools.py` | 9 个工具 + registry + JSON Schema 校验 |
| `src/ops_agent/cli.py` | 新增 `tools list` / `tools demo` |
| `data/compliance/lexicon.json` | 可替换的合规词库 |
| `tests/` | 42 个单元测试（新增 23 个） |

## 9 个工具

| 工具 | 副作用 | 需审批 | 用途 |
|---|---|---|---|
| `get_shop_snapshot` | read | - | 自家店铺最近 N 天的曝光/点击/转化/退货/毛利 |
| `get_competitors` | read | - | 客户授权的同类目参考数据（非抓取） |
| `get_trends` | read | - | 公开趋势指数，判断季节性 |
| `price_calc` | read | - | 毛利、价格带、毛利底线 |
| `generate_listing` | compute | - | 按假设生成 2–3 版 Listing 草稿 |
| `generate_assets` | compute | - | 图片 brief 与短视频脚本，标注为生成式素材 |
| `compliance_check` | read | - | 禁售/广告法/品牌词/版权四类检查 |
| `publish_listing` | write | 是（>10% 改价） | 发布，带版本与审批令牌 |
| `rollback` | write | - | 回滚到上一个已发布版本 |

## 关键决策

1. **工具是契约，不是函数。** 每个工具都有 JSON Schema、读写属性、审批要求；registry 在边界层校验参数，模型不用猜格式。
2. **副作用分三类。** `read` 只读；`compute` 生成但不落库；`write` 才改平台状态。权限最小化，审批只卡在真正落库的动作上。
3. **审批令牌一次性、绑定 SKU 与动作、会过期。** 发布工具自己校验令牌，不靠调用方自觉。
4. **竞品与趋势不抓取。** 竞品返回 `source=authorized_reference`，趋势返回 `source=public_trend_index`；每个结果都带 license 字段。
5. **合规规则与词库分离。** 词库是 JSON，换市场换类目只改数据，不改代码。
6. **发布版本化。** 每次发布生成版本号，回滚是弹出栈顶并恢复上一版本的价格。

## 难点与解决

**难点 1：工具异常不能穿透到 Agent。**
每个工具都返回统一的 `ToolResult(ok, data, error, code)`；registry 捕获 `ToolError`、审批异常和未知异常，Agent 只看 code 决定重试、换工具还是升级人工。

**难点 2：审批令牌不能变成“万能钥匙”。**
令牌绑定 `sku_id + action`，一次性消费，带 TTL；发布前先校验再消费，避免“校验通过但没消费”导致重放。

**难点 3：合规匹配的边界。**
`100%` 这类以符号结尾的词，正则尾部 `\b` 永远匹配不上（`%` 不是单词字符）。改成只对字母数字结尾加边界，`100%` 能命中，`pineapple` 也不会误伤 `apple`。

**难点 4：生成内容不能触发自己的合规。**
模板里主动避开绝对化用语、品牌词、“brand logo”这类版权词；否则 demo 会出现“生成完立刻被自己拦下”的尴尬，也说明模板设计没考虑合规。

**难点 5：发布与回滚的状态一致。**
发布时同时更新平台 SKU 价格与版本栈；回滚弹出栈顶并把价格写回上一版本。测试里断言回滚后价格等于原始价格，防止只改版本不改数据。

## Bug 记录

**Bug 1：`100%` 没被识别。**
第一次跑测试，`ComplianceTests.test_absolute_terms_are_blocked` 失败：`ABS-001` 没命中，只有 `ABS-002/003`。原因是 `re.search(r"\b100%\b")` 的尾部 `\b` 要求 `%` 后还是单词字符，永远不成立。
修复：`_contains_term()` 只对首尾的字母数字加 `\b`；补测试覆盖 `100%`。

**Bug 2：趋势首尾相同却标“Q4 上升”。**
`get_trends` 用 `>=` 判断，61 → 61 也标上升。改成 `>` / `<` / `==` 三分支，并补测试断言 seasonality 与首尾指数一致。

## 验收结果

- `python -m unittest discover -s tests -v`：42 个测试全部通过。
- `python run.py tools list`：9 个工具，读写/审批属性正确。
- `python run.py tools demo --sku SKU-1025`：8 步全部符合预期，其中第 6 步“无令牌发布”被拦截，第 7 步带令牌成功，第 8 步回滚到 v1。

## 设计问答

**Q1：为什么工具要有 JSON Schema，而不是直接传参数？**
A：Schema 是 Agent 与系统之间的契约。参数校验在边界层做，模型不用猜格式；后面接 function calling 时，这份 Schema 可以直接用。没有 Schema，参数错位会变成“模型能力问题”，很难排查。

**Q2：为什么生成类工具算 compute，不算 write？**
A：它只产出草稿，不改变平台状态。把“读 / 算 / 写”分开，权限最小化；审批只卡在真正落库的 `publish` 和 `rollback` 上，不然每生成一版都要人审，流程走不动。

**Q3：审批令牌为什么一次性、绑定 SKU/动作、会过期？**
A：防重放、防串用、防过期授权。令牌由发布工具自己校验并消费，调用方绕不过去；如果只靠“Agent 记得要审批”，等于没有控制。

**Q4：合规为什么用规则而不是纯 LLM？**
A：合规要可解释、可审计、可回归。规则命中能指向具体条款和修改建议，LLM 只能给“感觉有问题”。LLM 可以在后面做补充召回，但最终判定走规则。

**Q5：发布和回滚怎么保证可审计？**
A：每次发布生成版本号，记录审批令牌、时间、价格；版本栈落盘到 `platform_state.json`；回滚弹出栈顶并恢复上一版本。任何一次发布都能回放到“谁批的、改了什么、价格多少”。

**Q6：竞品数据哪来的，怎么合规？**
A：客户授权的第三方参考数据，不是页面抓取；返回里带 `source=authorized_reference` 和 `license=authorized`，下游只能消费带授权的数据。趋势用公开指数，带 `license=public`。

**Q7：工具调用失败了，Agent 怎么办？**
A：统一 `ToolResult` 带 code：`invalid_arguments` 改参数重试，`approval_required` 去申请人审，`compliance_failed` 回工坊改稿，`unknown_sku` 直接升级人工。Agent 不需要解析异常字符串。

## 下一步

Step 3：运营主管 Agent——体检、选品、诊断、生成 TaskOrder。先用规则 + 工具跑通，再接 LLM。
