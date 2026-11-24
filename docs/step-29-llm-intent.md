# Step 29 · 大模型意图识别引擎

把意图层从"纯规则"升级为"大模型优先、规则兜底"。规则版一个字没删——它是评测基准、CI
回归和大模型失败时的安全网。

## 1. 架构

```
委托文本
   │
   ├─ INTAKE_ENGINE=rules ──────────────► 规则版 classify（原样保留）
   │
   └─ auto（默认）/ llm
        │
        ├─ 冷却期内（5 分钟前刚失败过）────► 规则版（一次请求都不发）
        │
        └─ LLMClient.chat_json（temperature=0，max_tokens=400）
             │
             ├─ 输出合法 ────────────────► IntentResult(engine="llm")
             │                             missing 由程序按规格表重算
             └─ 意图非法 / 网络失败 / JSON 坏 / 欠费 429
                  │
                  └─► 规则版回退，engine="rules(fallback)"，并进入 5 分钟熔断
```

| 文件 | 角色 |
| --- | --- |
| `intake/llm_router.py`（新增） | prompt 生成、输出消毒、引擎选择、熔断 |
| `intake/schema.py` | `IntentResult` 新增 `engine` 字段（rules / llm / rules(fallback)） |
| `console/app.py`、`cli.py` | 活路径改用 `classify_auto`；响应与委托历史都带 `engine` |
| `scripts/eval_intake.py` | 新增 `--engine llm`（对 204 条语料真实调用模型，可对比规则准确率） |
| `tests/test_intake_llm.py`（新增） | 20 条离线契约测试（注入假 transport，零网络） |

## 2. 安全设计（模型输出永远不可信）

- 意图必须命中 12 个枚举之一，否则整条输出判废、回退规则；
- 槽位键白名单：只放行规格表声明的槽位 + `quantity`，模型塞进来的
  `admin` / `raw_sql` 之类直接丢弃；
- `market` 统一大写、`language` 统一小写、`quantity` 强转 int；
- `missing` 由程序按 `INTENT_SPECS` 重算，不信模型自己报的"缺什么"；
- `candidates` 只留合法意图名、排除主意图、最多 2 个（与规则版口径一致）；
- 置信度 clamp 到 [0, 1]。

## 3. 成本与可用性

- temperature=0、max_tokens=400、输入截断 2000 字、空输入不调模型；
- prompt 从 `INTENT_SPECS` 自动生成 + 7 条 few-shot，规格表改了 prompt 跟着变；
- **熔断**：模型失败（欠费 / 限流 / 超时）后 5 分钟内不再尝试，后续委托直接走
  规则——欠费的 key 不能让每一次委托都白等一轮重试；成功即清除。

## 4. 引擎选择

`.env` / 环境变量：

```
INTAKE_ENGINE=auto     # 默认；有可用大模型就走模型，失败回退规则
INTAKE_ENGINE=llm      # 强制模型（失败仍回退，控制台永不 500）
INTAKE_ENGINE=rules    # 强制规则（评测/CI 默认行为）
```

当前 `.env` 配的是 MiniMax（`MiniMax-M2.7-highspeed`），实测返回 429
「已达到 Token Plan 用量上限」。充值后无需改代码即可生效；换供应商只需改
`LLM_PROVIDER` / `LLM_API_KEY`（DeepSeek：`deepseek` / `sk-...`）。

## 5. 验证

```
python -m unittest tests.test_intake_llm        → 20 条，OK（全离线）
python -m unittest discover -s tests            → 899 tests, OK (skipped=1)
python scripts/eval_intake.py --no-report       → 规则版指标不变（98.53%）
python run.py intake run --text "..." --dry-run → 响应带 engine 字段
```

`--engine llm` 的对比评测需要可用额度后跑：`python scripts/eval_intake.py --engine llm --no-report`。
