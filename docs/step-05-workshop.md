# Step 4 — 内容工坊：TaskOrder → 2–3 版可发布方案

## 本步目标

把运营主管的 TaskOrder 变成可执行的方案：每个 SKU 出 2–3 版，每版有文案、卖点、价格、图片 brief、预期提升，并且全部过合规、带 before/after 预览。

先用规则模板跑通；后面把模板换成 LLM，输出结构不变。

## 交付物

| 文件 | 作用 |
|---|---|
| `src/ops_agent/workshop.py` | 角度模板、价格点、合规检查、预览 |
| `src/ops_agent/cli.py` | 新增 `workshop run`（离线 / 实时） |
| `tests/test_workshop.py` | 9 个方案与合规测试 |
| `data/workshop_proposals.json` | 真实店铺生成的方案包 |

## 输入输出

输入：

- `data/task_orders.json`：运营主管的 5 个 TaskOrder
- `SiteProduct`：商品、变体、价格、成本、MOQ、交期
- 实时模式下额外的商品详情（英文标题 / 卖点）

输出：

- 每个 SKU 2–3 版方案；
- 每版：角度、标题、4 条卖点、描述、价格、图片 brief、视频脚本、预期提升、合规结果、before/after 预览。

## 关键决策

1. **每个问题 2–3 个角度，不堆同质方案。**
   - 下单低：阶梯价 / 低起订 / 快交期
   - 询盘低：认证质检 / 应用场景 / 规格尺寸
   - 流量低：关键词 / 类目属性 / 卖点前置
   - 差评高：质量证据 / 描述对齐 / 售后
   - 库存积压：清仓组合 / 价值组合
2. **价格取任务带的低 / 中 / 高三点。**
   先取 TaskOrder 的 `price_band_cents`，再用 `成本/(1-16%)` 抬下限；三个价格点覆盖“激进 / 中间 / 保守”。
3. **每版必须过合规。**
   模板主动避开绝对化用语、品牌词、版权词；生成后仍走 `ComplianceChecker`，不通过就标记 violations，不进入可发布清单。
4. **before/after 预览是产品的一部分。**
   商家批准前要看到“标题从什么变成什么、卖点替换了哪几条、价格从多少到多少”；实时模式还能用连接器的 dry-run 再核一遍。
5. **离线与实时同一套生成逻辑。**
   都调用 `build_proposals`；区别只在 before 的英文卖点：实时能从详情接口取，离线快照里没有。

## 难点与解决

**难点 1：模板要一次过合规。**
“100%”“best seller”“guaranteed”“brand logo”这些词一旦进模板，9 个版本会全军覆没。解决：模板只用可证伪的表述（tier pricing、QC report、CE/RoHS、real photos），并在生成后强制跑一遍合规检查。

**难点 2：价格既要覆盖任务带，又不能破毛利线。**
任务带可能低于成本线。解决：先把任务带下限抬到 `ceil(cost / (1 - margin_floor))`，再在抬过的带内取低/中/高三个点；测试断言每个价格点都 ≥ floor。

**难点 3：实时和离线的 before 不一致。**
实时能拿到英文卖点，离线快照只有标题和价格。解决：实时把 `i18n.En.bullets` 填进 before；离线 before 卖点为空，文档里写明，避免误以为“原来没有卖点”。

**难点 4：英文 brief 混进中文。**
第一版把中文角度名拼进 image brief，生成了“阶梯价 scene in natural light”。解决：brief 用英文 suffix（`Scene for Bulk Pricing Available: ...`），中文只留在运营台的展示字段。

**难点 5：只出 brief，不出图。**
直接生成图片会有版权、品牌、人物肖像风险。解决：只输出图片 brief 和视频脚本，标注 `ai_generated`，由素材工具或人来执行。

## Bug 记录

**Bug 1：英文 image brief 混入中文角度名。**
`f"{angle_label} scene..."` 把“阶梯价”拼进英文 brief。修复：改用英文 `suffix`。

**Bug 2：描述里 “Tier pricing” 重复。**
角度 pitch 已经说了 tier pricing，固定尾句又说一遍。修复：尾句改成 “MOQ, lead time and QC documents are available on request.”

## 验收结果

- `python -m unittest discover -s tests -q`：71 个测试通过（新增 9 个）。
- `python run.py workshop run --offline --limit 3`：3 个方案包，每个 3 版，9/9 合规通过。
- `python run.py workshop run --limit 3`：实时结果与离线一致。
- `data/workshop_proposals.json`：3 个方案包、9 个版本，含 preview。

真实输出示例：

| # | 商品 | 问题 | 方案数 | 合规 | 价格带 |
|---|---|---|---|---|---|
| 1 | 350ml Ceramic Coffee Mug Set of 4 | 下单低 | 3 | 3/3 | $5.58–6.82 |
| 2 | 300TC Cotton Bed Sheet Set | 下单低 | 3 | 3/3 | $19.71–24.09 |
| 3 | 10m Solar String Light | 询盘低 | 3 | 3/3 | $6.84–8.36 |

## 设计问答

**Q1：为什么先模板，不直接让 LLM 写？**
A：模板能保证合规、价格和结构一次到位，也方便回归测试。LLM 的价值在“同一个卖点写出不同风格”，但底线（不绝对化、不破毛利线、必须有 before/after）不能交给它。先用模板把结构和评测跑通，再让 LLM 在模板约束内改写。

**Q2：三个角度怎么选，怎么保证不同质？**
A：按问题类型分：下单低看价格/起订/交期，询盘低看认证/场景/规格，差评高看质量/描述/售后。每个角度对应不同的卖点首条和标题后缀，不是同一句话换三个说法。

**Q3：价格怎么定，怎么保证不亏？**
A：取任务带的低/中/高三点；下限先抬到 `成本/(1-16%)`。测试断言每个价格点都 ≥ floor，且落在任务带内。

**Q4：怎么保证 9 个版本都过合规？**
A：模板主动避开禁用词，生成后再跑 `ComplianceChecker`；不通过就标记 violations，不进入可发布清单。测试断言 9/9 通过。

**Q5：before/after 从哪来，dry-run 怎么接？**
A：实时模式从商品详情取英文标题/卖点，离线模式从快照取标题和价格；preview 记录 before/after。发布前再调用连接器 `patch_product?dryRun=true` / `patch_variant?dryRun=true` 拿服务端 diff，双重确认。

**Q6：为什么只出 brief，不出图？**
A：生成图片涉及版权、品牌、人物肖像；当前先把 brief 和脚本标准化，素材由 AI 工具或人执行。等素材链路稳定后，再把生成接进来。

## 已知限制

1. 方案只生成英文；站点支持 7 种语言，多语言版本还没接。
2. 合规检查固定用 `US` 词库；DE/UK 的差异还没做。
3. 离线快照不存英文卖点，before bullets 为空。
4. 不生成真实图片/视频，只出 brief 和脚本。
5. 方案还没有写回站点；发布编排（dry-run → 审批 → 幂等写入）是下一步。

## 下一步

Step 5：合规审查 Agent + 发布编排。

- 合规审查独立成角色，可否决；
- 发布用连接器的 dry-run → 人工审批 → 幂等写入 → 版本回滚；
- 把三个角色（主管 / 工坊 / 合规）串成一条流水线。
