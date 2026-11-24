# Step 11：多 Agent 协作与大模型自主决策

## 1. 当前能力

Step 4–6 是固定接力：主管出工单、工坊套模板、合规检查、审批后发布。Step 11 把业务判断整体交给三个 Agent，代码只保留取数、契约、配额、红线与审批：

- **P0（已完成）**：契约、配额、记忆三块地基。`contracts.py` / `quota.py` / `memory.py`。
- **P1（已完成）**：运营主管 `agents/director.py`。一次调用完成体检、选品、诊断、假设、价格带与派活；改几个商品由模型按店铺规模决定，代码只做区间护栏。旧的阈值选品只保留在 `--planner rule` 对照基线里。
- **P2（已完成，见 step-12）**：内容工坊 `agents/workshop_agent.py` 按任务单出 2–5 版并自评修订，失败按错误清单重写而不回落模板；合规 `agents/compliance_agent.py` 做四类检查 + 否决权 + 硬禁代码兜底。
- **P3（待做）**：记忆与实验。发布即登记实验、到期判定成立/不成立/样本不足，经验库回灌主管。
- **P4（待做）**：运营台体检面板、实验看板、记忆浏览。

它不是不受限制的 ReAct 机器人。模型在代码算出的边界内做判断，写入工具永远不对模型开放，发布仍需人工令牌。

## 2. 角色分工

| 角色 | 代码 | 决定什么 | 不能决定什么 |
|---|---|---|---|
| 运营主管 | `agents/director.py`（P1 已上线） | 店铺体检、改几个、改哪几个、主问题、假设、目标指标、价格带、每商品几版 | 商品事实、成本底线、审批容量、发布 |
| 契约与配额 | `contracts.py` / `quota.py` | 任务单字段是否合格、数量与版数是否越界、价格是否低于成本 | 不选商品、不选问题 |
| 内容工坊 | `workshop.py` + `agents/workshop_agent.py`（P2 已上线） | 版数、角度、2–5 版标题/卖点/描述/素材 brief；自评修订 | 改任务单、改价格档位、改其他字段 |
| 合规审查 | `compliance.py` + `agents/compliance_agent.py`（P2 已上线） | 四类合规判定、可否决；硬禁由代码覆盖 | 不参与创作、不覆盖人工审批 |
| 红线守卫 | `agents/base.py` | 丢弃越权字段、非英文文案、无法溯源的规格数字 | 不参与创作 |
| 审批人（人） | 运营台 | 是否允许写入 | 不由模型替代 |

当前协作顺序（P1）：

```text
主管：体检 → 决定数量 → 选品 → 诊断 → 假设 → 价格带 → TaskOrder
      ↓（契约校验 + 配额裁剪，不合格先自修一次）
工坊：策略定角度 → 文案写 N 版 → 评审打回重写（最多 2 轮）
      ↓
红线守卫 → 规则合规 → 人工审批 → 发布
```

## 3. 策略自主的边界

规则诊断会保留本商品实际触发的所有问题类型，例如 `high_negative`、`low_order`。Strategist 只能从这份集合中选择，不能凭空说商品“库存积压”或“毛利低”。

每个问题选项都已经由代码算好：

- 价格带与 16% 毛利底线；
- 低 / 中 / 高三个价位点；
- 规则证据与默认动作。

模型输出的是 `price_index`，不是金额。即使它返回 `price_cents: 1`，该字段也不会进入策略。文案阶段如果私自改变策略官指定的角度或 `price_index`，Orchestrator 会改回策略值并记录 `aligned to strategist`。

**选品仍由规则排序**。这样可以把模型调用集中在最有价值的候选商品上，也避免模型无依据地忽略高风险商品。

## 4. 红线

| 类别 | 具体限制 | 越界后果 |
|---|---|---|
| 可改写字段 | 只有 `titleEn` / `bulletsEn` / `descEn` | 其他字段丢弃并记 `violations` |
| 禁写字段 | `status` `slug` `costCents` `skuCode` `stock` `moq` `hsCode` 等 | 同上 |
| 问题选择 | 只能选规则实际触发的问题 | 整次策略回复无效，保留规则诊断 |
| 价格 | 只能选代码给出的 `price_index` | 越界强制为 0；文案阶段再按策略复核 |
| 模型自带金额 | `price_cents` 不采信 | 实际金额永远来自代码 |
| 评价证据 | 只能引用输入评价的索引 | 非法索引丢弃，证据文本取自真实评价 |
| 语种 | `title` / `bullets` / `description` 必须是英文 | 出现中文字符整版丢弃，回落规则英文方案 |
| 数字可溯源 | 文案里的规格数字必须出现在已知事实（现有标题、卖点、描述、MOQ、交期）里 | 带单位的规格子弹整条丢弃；标题或描述含未授权数字则整版丢弃 |
| 工具 | 只允许 `read` / `compute` | 发布、回滚工具不开放 |
| 轮次与预算 | 单商品策略 + 文案 + 评审总计 ≤6 次调用，修订 ≤2 轮 | 强制收敛 |
| 合规与发布 | 规则合规、人工令牌 | 模型无权绕过 |

## 5. 配置：换模型只改 .env

```ini
LLM_PROVIDER=deepseek      # deepseek | qwen | glm | moonshot | minimax | openai | ollama
LLM_API_KEY=               # 留空 = 只用规则，不发起任何外部请求
LLM_MODEL=                 # 留空 = 用 provider 默认模型
LLM_BASE_URL=              # 留空 = 用 provider 默认地址
LLM_MAX_CALLS_PER_ITEM=6
LLM_MAX_REVISION_ROUNDS=2
```

| provider | 默认 base_url | 默认模型 | 需要 key |
|---|---|---|---|
| deepseek | `https://api.deepseek.com/v1` | deepseek-chat | 是 |
| qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-plus | 是 |
| glm | `https://open.bigmodel.cn/api/paas/v4` | glm-4-plus | 是 |
| moonshot | `https://api.moonshot.cn/v1` | moonshot-v1-8k | 是 |
| minimax | `https://api.minimaxi.com/v1` | MiniMax-M2.7-highspeed | 是 |
| openai | `https://api.openai.com/v1` | gpt-4o-mini | 是 |
| ollama | `http://127.0.0.1:11434/v1` | qwen2.5:14b | 否 |

所有厂商都走 OpenAI 兼容的 `/chat/completions`。环境变量优先于 `.env`，临时切模型不用改文件。

推理模型的隐藏思考 token 也计入 `max_tokens`。预算被思考耗尽时接口会返回空正文（`finish_reason=length`），客户端会自动把额度放大到 4096–8192 重试一次；`.env` 里 `LLM_MAX_TOKENS` 建议不低于 4096，`LLM_TIMEOUT` 建议 120（单次真实调用约 5–40 秒）。部分型号（如 MiniMax M2.x highspeed）把思考写在正文的 `<think>` 标签里，客户端会在解析前剥离。

## 6. 使用

```bash
# 运营主管：一次调用完成体检、选品、诊断、假设、价格带、派活
python run.py director run                       # 数量 auto，由模型按店铺规模决定
python run.py director run --limit 4             # 人工上限 4 个（仍受成本与审批容量约束）
python run.py director run --no-memory           # 演练，不读写记忆

# 主管 + 工坊一条命令
python run.py agent run

# 只按已有 TaskOrder 重跑文案
python run.py workshop run --planner llm

# 旧规则基线（对照用，不参与模型链路）
python run.py agent run --planner rule
```

`director run` 的行为边界：

| 事项 | 谁决定 | 代码做什么 |
|---|---|---|
| 改几个商品 | 模型 | `clamp(ceil(N×10%), 3, 20)` 与 `review_cap/2` 取最紧，越界写 `guarded` |
| 改哪几个 | 模型 | 只校验 `product_id` 存在于快照，未知商品丢弃 |
| 主问题 | 模型（可自创类型） | 只要求 `problem + problem_detail + ≥2 条证据` |
| 价格带 | 模型给区间 | 不得低于 `cost/(1-16%)`，不得超出当前价 50%–300% |
| 每商品几版 | 模型 | 2–5，且不超过审批容量摊到每个商品的上限 |
| 价位点具体金额 | 代码 | 在带内等距取 N 个点，模型只能选下标 |

模型首版不合格时，代码把错误清单回传让它自修一次；修不好就丢弃该商品，全部不可用则整批中止。**不产出规则兜底方案**。真实站点模式下，主管只为有差评记录的商品读取 `/api/agent/products/:id` 的 `reviews`（最多 20 个），负面评价优先；离线快照没有评价正文。

## 7. 数据契约（只增不改）

`task_orders.json` 的每条任务新增：

```jsonc
{
  "planner": "llm",
  "strategy": {
    "problem": "high_negative",
    "hypothesis": "色差导致买家不信任图片",
    "action": "补自然光实拍并校准描述",
    "reason": "两条差评都提到颜色差异",
    "confidence": 0.86,
    "angles": [
      { "angle": "真实颜色实拍", "focus": "展示自然光釉色", "price_index": 1 }
    ]
  }
}
```

`workshop_proposals.json` 继续包含 `planner`、`critique`、`revisions`。原有商品、变体、预览和发布字段不变，因此审查、发布和运营台无需修改。

每次模型调用落在 `data/llm_trace/{run_id}.jsonl`：角色、prompt、回复、token、耗时和失败原因。

## 8. 降级矩阵

| 情况 | 行为 |
|---|---|
| key 为空 | `director run` 直接报错退出，不产出规则方案；要基线请显式 `--planner rule` |
| 主管返回坏 JSON / 被截断 | 截断识别为预算问题，客户端扩容重试一次（最高 16384）；仍失败整批中止 |
| 主管字段不合格 | 回传错误清单自修一次；仍不合格丢弃该商品；全部不可用则整批中止 |
| 主管网络失败 | 整批中止，保留 trace 与记忆快照，不写半份 TaskOrder |
| 数量越界 | 裁到区间上限，产物记 `guarded=true` 与原始请求值 |
| 价格低于成本 | 抬到 `cost/(1-16%)`，并在 `violations` 记录 |
| 文案模型失败 | 按红线/契约错误重写最多 2 轮；仍失败则该商品失败并记入 `failures[]`，不回落模板 |
| 字段或价格越权 | 修正 / 丢弃并留 `violations` |
| 预算耗尽 | 截断被识别为预算问题，扩容重试；二次失败才报错 |

主管链路没有"静默降级"：模型不可用就是整批中止，避免界面上出现"成功了但其实是模板"。

## 9. 验收

```bash
python -m unittest discover -s tests      # 351 个用例
```

P2 专项测试见 `docs/step-12-p2-workshop-compliance.md`：版数区间与裁剪、自评缺失重写、编造数字丢弃、四类合规、硬禁覆盖、无依据否决丢弃、发布与审批闸门。

人工验收看五条：

1. 差评提到“色差”，策略与文案出现实拍、颜色校准等对应动作，而不是泛泛补认证。
2. 同一商品的不同版本有不同打法，不只是换同义词。
3. `price_cents` 全部来自代码价位点，且与策略的 `price_index` 一致。
4. 模型返回不存在的问题或金额时，不会进入发布产物。
5. `data/llm_trace/` 能还原每次策略、文案和评审调用。

## 10. 运营台溯源（P3）

运营台接口在每份 TaskOrder 和 Proposal 上附加只读的 `agent_meta`，源文件不被改写：

```jsonc
{
  "source": "model",              // model | fallback | rule | legacy
  "label": "模型生成",
  "planner": "llm",
  "recorded": true,
  "strategy": {
    "problem": "high_negative",
    "problem_label": "差评高",
    "hypothesis": "...",
    "action": "...",
    "confidence": 0.86,
    "angles": [{ "angle": "真实实拍", "focus": "...", "price_index": 1 }],
    "evidence": ["评价原文：..."]
  },
  "critique": { "score": 0.82, "verdict": "pass", "issues": [] },
  "revisions": 1
}
```

界面上：

- 方案卡显示来源徽标、合规比例、评审分与结论；
- 详情区显示模型策略、判断、动作、预选角度、可折叠证据、评审意见与修订轮次；
- 本周动作表增加来源列，并在动作下带最多两条决策证据；
- 顶部“方案”指标显示模型 / 兜底 / 规则 / 历史各自数量。

旧版本生成的文件没有 `planner` 字段，会标为“历史数据 · 来源未记录”，不会伪称由规则或模型产出。来源是信息标注，不改变审批与发布权限。

浏览器验收：390px 移动端页面无横向溢出，宽表在自身容器内滚动；模型输出在渲染前全部经过 HTML 转义。

## 11. 尚未实现

- **P3 记忆与实验**：发布即登记实验、到期判定成立/不成立/样本不足、经验库回灌。
- **P4 运营台**：体检面板、实验看板、记忆浏览。
- 模型自主扩大候选范围（目前由全量快照直接进入模型，不再做规则初筛）。
