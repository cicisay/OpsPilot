# Step 12：P2 工坊与合规 —— 多版生产、自评、四类否决

## 1. 这一步做了什么

P1 把"改几个、改哪个、为什么"交给了运营主管。P2 把剩下的两段也交出去：

| 环节 | P1 状态 | P2 之后 |
|---|---|---|
| 内容工坊 | 固定角度表 + 固定 3 版，模型只写词；失败回落模板 | 模型在任务单区间内自定 2–5 版，自评三项，按红线/契约错误定向重写；**不回落模板** |
| 合规审查 | 词表逐条匹配，无四类结论，无否决语义 | 一次审完一个商品的全部版本，C1–C4 四类结论 + 依据 + 建议；`block` 不可批 |
| 否决链路 | 只有 `passed` 布尔值 | `review_results.json` 记 `severity/checks/findings`；发布、审批、运营台三处同时拦截 |

新增文件：

- `src/ops_agent/agents/workshop_agent.py`：工坊 Agent，版数决策 + 自检 + 定向重写。
- `src/ops_agent/agents/compliance_agent.py`：合规 Agent，四类检查 + 代码硬禁 + fail-closed。
- `tests/test_workshop_agent.py`（32 例）、`tests/test_compliance_agent.py`（24 例）、`tests/test_gate.py`（11 例）。
- `scripts/p2_compliance_probe.py`：真实模型合规探针（5 种绝对化改写 + 品牌硬禁 + 干净对照）。

修改：`workshop.py`（llm 路径改走新 Agent，删除规则回退）、`contracts.py`（`variant_key`/`self_check` 透传）、`agents/base.py`（红线保留 `material_brief`/`self_review`/`self_check`）、`publish.py`（否决闸门）、`cli.py`（`workshop/review/agent run` 与 `approve`/`publish` 接入）、`console/app.py`（令牌签发前二次校验）。

## 2. 协作顺序

```text
运营主管：体检 → 数量 → 选品 → 诊断 → 假设 → 价格带 → TaskOrder
      ↓ 契约 + 配额
内容工坊：读任务单 → 定版数（区间内）→ 每版一个角度 → 英文文案
          → 三项自检 → 有错定向重写（最多 2 轮）
      ↓ 红线 + 契约（英文 / 数字可溯源 / 价位点）
合规审查：一个商品一次调用 → C1 绝对化 / C2 IP / C3 禁售资质 / C4 市场法规
          → 代码取最严 severity → 硬禁覆盖 → 无结论版本丢弃
      ↓
人工审批：逐版看；block 版本不出现、不可批
      ↓
发布执行器：合规通过 + 有效令牌 + 商品锁，三者同时满足才写入
```

调用预算：主管 2 次/批（决策 + 一次自修），工坊 1–3 次/商品（起草 + 最多 2 轮重写），合规 1–2 次/商品（审查 + 缺结论时补审）。写入类工具仍然永远不给模型。

## 3. 工坊：版数与自评都归模型

### 3.1 版数

任务单只给区间（`variant_min..variant_max`，2–5），模型自己选并给理由。代码做三件事：

- 超过上限：裁剪并在 `issues` 记 `X variants produced, trimmed to variant_max Y`，**不触发重写**（裁剪后仍合格，重写只会浪费一次调用）。
- 低于下限：记问题并重写；重写后仍不足则整单失败，不拿 1 版凑数。
- 区间本身非法（如 1..9）：`variant_range()` 先夹到 2..5，倒置区间收敛到下界。

### 3.2 每版必须交的东西

| 字段 | 要求 | 谁检查 |
|---|---|---|
| `angle` | 每版一个角度，标题上能看出差异 | 契约（重复角度拒绝） |
| `title` / `bullets` / `description` | 全英文；2–6 条卖点 | 红线 + 契约 |
| 数字 | 只能来自现有标题/卖点/描述/MOQ/交期 | 红线（无溯源即丢） |
| `price_index` | 只能选代码给的价位点下标 | 红线（越界归 0） |
| `material_brief` | 1–3 条：主体、字幕、禁用元素 | 工坊自检 |
| `self_check` | `facts` / `differentiation` / `readability` 三项 | 工坊自检，缺失触发重写 |
| `self_review` | 一句话说明自检修了什么 | 契约字段 |

### 3.3 失败就重写，不换模板

旧实现里模型写坏会悄悄回落规则模板，产物标 `rule (fallback)`。问题是运营台只看到"有方案"，看不出这是另一条代码路径；P1 真实跑批时就有 1 个商品因为编造 `46dB / 3500 cycles` 被红线整版丢弃，然后被模板顶上。

P2 改为：

1. 红线与契约把问题列成清单（哪一版、哪个字段、什么原因）。
2. 原清单 + 上一版可用内容回传模型，要求只修这些问题。
3. 最多 2 轮；仍不合格则该商品失败，`workshop_proposals.json` 的 `failures[]` 记录原因。

真实跑批验证了这条路径：第 4 个商品首版 JSON 被截断，工坊带解析错误重试一次后产出 3 版（`attempts=2`），而不是回落模板。

## 4. 合规：四类结论 + 代码硬禁

### 4.1 四类检查

| 键 | 查什么 | 典型 block |
|---|---|---|
| `c1_absolute` | 绝对化、承诺、疗效 | `100% effective`、`guaranteed`、`zero failure`、`包您满意` 的英文改写 |
| `c2_ip` | 商标、名人、影视/动漫、logo | `Nike-grade`、`Disney style` |
| `c3_category` | 禁售类目、认证缺失或虚构 | 类目在禁售清单、文案声称未授权的 CE/FCC |
| `c4_market` | 目标市场本地化法规 | 德国 `klimaneutral`、未授权 `TÜV`、英国 `risk-free` |

模型只报四类状态，`severity` 由 `contracts.build_verdict()` 取最严的一类。模型自报 `pass` 但某一类 `block` 时被代码推翻。

### 4.2 代码硬禁（Q1 的结论：保留）

`CAT-001`（禁售类目）、`BRD-001`（他人品牌）、`CPY-001`（版权形象）命中时，无论模型怎么判，代码强制四类全 `block`，并记 `overridden_by=code`。

绝对化词（`ABS-*`）**不**自动 block：`100% cotton` 是成分，不是承诺。词表命中只作为证据交给模型判断语境，避免误杀（FR-521）。这条边界由 `test_absolute_term_does_not_force_a_block` 固定。

### 4.3 Fail-closed

- 某版本没有结论 → 丢弃，不进审批队列；先补审一次，仍缺才丢。
- 四类里缺任意一类 → 视为未完成，同样丢弃。
- `block` 但没有 `findings` → 视为不可复核的结论，丢弃（FR-519：不能只给"不合规"三个字）。
- 一个商品所有版本都被丢 → 该商品本次无方案，`review run` 汇总里计"无结论"。

### 4.4 否决链路

```text
review_results.json（severity=block）
   ├─ publish preview → blocked=true
   ├─ publish 执行    → status=blocked，令牌不消费，站点零写入
   ├─ approve issue   → 拒绝签发令牌（exit 1）
   └─ 运营台 approve  → 409，提示先重做再审
```

旧格式 `review_results.json` 只有 `passed` 布尔值：`passed=false` 一律映射为 `block`，`passed=true` 映射为 `pass`。这样升级后旧文件不会意外解锁任何版本。

### 4.5 词库只有一个入口

词库是运营唯一能自己改的合规配置，所以它必须只有一个解析入口：

```text
OPS_LEXICON_PATH  >  tenants/<slug>/compliance/lexicon.json  >  data/compliance/lexicon.json
```

`ComplianceChecker()` 无参构造（合规 Agent、工坊自检、词表基线都走它）和 `_build_context()`（工具链）现在都调用 `default_lexicon_path()`，不再出现"运营改了文件、Agent 还在读内置副本"。

磁盘词库与内置基线是**分层**关系，不是替换关系：

| 情况 | 处理 | 理由 |
|---|---|---|
| 文件缺某个检查类键（如旧版没有 `market_terms`） | 从基线补齐，并返回补了哪些键 | 旧文件不代表运营想关掉整类规则 |
| 键存在但值为空（`"brand_terms": []`） | 原样保留，不补 | 这是运营的显式决定，不能被程序悄悄推翻 |
| 键存在且有内容 | 完全以磁盘为准，逐条不合并 | 运营改词就要生效 |
| 文件损坏/不可读 | 回落内置基线 | 合规失败要往严的方向倒 |

补齐是可见的：`review run` 发现缺键会打印 `[WARN] 词库缺少 market_terms，已按基线补齐`，同时把补齐后的内容写回文件。内容无变化时不重写，避免每次审查都改文件时间戳。

## 5. 真实运行（MiniMax-M2.7-highspeed）

### 5.1 工坊

```text
任务单 4 个 · 生成 4 个方案包 · planner=llm
#  商品                          版数  词表  价格带            尝试
1  Double-Wall Vacuum Bottle      3    3/3   $3.90–3.90        1
2  87-Key Hot-Swap Keyboard       3    3/3   $24.99–27.99      1
3  Custom Printed Kraft Mailer    2    2/2   $1.65–1.95        1
4  Portable Power Station         3    3/3   $189.00–205.00    2
工坊调用 5 次 · token 27,162 · 194 秒
```

- 12 个版本全部为英文，`material_brief` 3 条、`self_check` 3 项齐全。
- 第 4 个商品首版 JSON 截断，重试一次后成功；没有回落模板。
- 版数分别 3/3/2/3，符合任务单区间，没有一律顶到上限。

### 5.2 合规

```text
4 个商品 / 11 个版本 · 调用 4 次 · token 9,563 · 41 秒
否决 0 · 提示 3 · 通过 8 · 无结论 0
```

3 条 `warn` 全部来自键盘的 `c3_category`（认证信息未在 listing 中声明），属于"可发但需人工确认"，不是否决。

### 5.3 合规探针

```text
abs-1  否决        100% effective
abs-2  否决        Guaranteed to work every single time
abs-3  否决        Zero failure rate
abs-4  否决        Absolutely effective, no exceptions
abs-5  否决        You will be satisfied, we promise
brand  否决 · 代码强制   Nike-grade grip
clean  通过        Stoneware mug with reactive glaze

绝对化识别 5/5 · 品牌硬禁 block · 干净文案 pass · 1 次调用 · 7 秒
```

## 6. 这一步的痛点与解决

| 痛点 | 现象 | 解决 |
|---|---|---|
| 规则回退掩盖失败 | 模型写错也有"方案"，运营台看不出是模板 | llm 路径不再回退；失败进 `failures[]`，测试 `test_llm_failure_is_reported_not_replaced_by_rule` |
| 截断 JSON 丢商品 | 首轮真实跑批第 4 个商品直接失败 | 捕获解析错误，带原始错误与"描述 ≤60 词"重试一次；第二次真实跑批 4/4 |
| 超出上限被静默裁剪 | 红线 `limit` 先截断，`issues` 里看不到模型要了几版 | 裁剪前先数原始数组，记 `requested > variant_max`；裁剪不触发重写 |
| `variant_key` 过不了红线 | 红线只回白名单字段，版本标识丢失，发布对不上 | 红线透传 `material_brief/self_review/self_check`；契约同时接受 `variant_id`/`variant_key` |
| 缺一类检查就放行 | 旧实现只要没命中词表就算过 | 四类必须齐全；缺失即 dropped，不进入队列 |
| block 没有依据 | 模型只回"不合规" | `block` 且 `findings` 为空 → dropped |
| 旧审查文件解锁新链路 | 旧格式没有 severity | `passed=false → block`，`passed=true → pass` |
| 令牌绕过否决 | `approve issue` 只看角色 | 签发前查 verdict，block 拒绝；运营台 409；发布层再拦一次 |
| 绝对化误杀 | `100% cotton` 被词表判死 | 只有 CAT/BRD/CPY 硬禁，绝对化交模型判语境 |
| 词库静默失效 | 磁盘词库是旧版、没有 `market_terms`，DE/UK 五条市场规则对 33/50 个商品完全不触发；且 Agent 默认构造压根没读这个文件 | 新增 `default_lexicon_path()` 单点解析；`merge_lexicon()` 缺键补基线、显式清空尊重运营；补齐时 WARN 并写回 |

## 7. 测试

```bash
python -m unittest discover -s tests
# Ran 356 tests · OK（P1 284 + P2 新增 72）
```

P2 专项覆盖：

- 版数区间、倒置/越界收敛、模型自选 2/3 版、超出上限只裁不重写、低于下限重写后仍失败则报错。
- 红线：中文整版丢弃、标题编造数字整版丢弃、卖点编造数字只删该条、`price_index` 越界归 0。
- 自评：三项缺失触发重写；`material_brief` 为空/超 3 条记录问题。
- 合规：四类齐全、缺一类丢弃、缺版本丢弃、block 无依据丢弃、嵌套 `checks` 兼容、按位置回退、重复键保留首条。
- 硬禁：品牌/禁售类目强制 block 且 `overridden_by=code`；绝对化不强制。
- 闸门：block 预览标记、带令牌也不写入、warn 可发布、旧格式映射、`approve issue` 拒绝 block。
- 真实链路：首版坏 JSON 重试、两次坏 JSON 报模型错误、调用预算收敛。
- 词库：旧版缺 `market_terms` 仍命中 DE-001、显式空节不补、运营新增词覆盖基线、损坏文件回落基线、`ensure_lexicon` 补键并幂等。

## 8. 已知边界

- 素材 brief 仍是文字，不生成图片（Q5 未变更）。
- 合规 `warn` 没有分级展示，运营台在 P4 才会把它做成显著标记。
- 工坊允许不同版本选同一价位点；价格差异化不是硬约束，P3 可结合实验数据再收紧。
- 工坊失败的商品本周没有方案；没有"下一轮自动重试队列"，需要重跑 `workshop run`。
- 经验库尚未回灌（P3）。

## 9. 下一步（P3）

- 发布即登记实验：`experiment_id = product_id + variant_id + 开始日期`，基线取发布前 28 天。
- 到期判定：成立 / 不成立 / 不确定 / 样本不足，写 `experiments.jsonl` 与 `lessons.json`。
- 主管读判定结果，下一周决策引用"上次同类改动是否有效"。
- 运营台实验看板与记忆浏览（P4）。
