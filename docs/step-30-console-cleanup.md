# Step 30 · 运营台摘除委托台与事实面板

> 执行时间：2026-11-12
> 约束：**只摘前端 UI，不动意图层与接口**。后端一行未删。
> 回归：`python -m unittest discover -s tests -q` → **889 OK**（原 899，删掉 10 条前端静态对照）。

---

## 1. 为什么做

运营台的主线是「体检 → 选品 → 出方案 → 审批 → 发布」。委托台（Step 5）是改造阶段加的第二个入口：
一句话 → 意图 → 计划预览，只读、不执行。

实际使用时它带来两个问题：

1. **两个入口，心智分裂**：同一个"挑商品"的动作，用户既能在"本周动作"里看主管选的结果，也能在委托台里说一句话看预览，两边数据不连通。
2. **它不产生任何业务数据**：`intake_preview()` 只读快照、写一条 `intake/history.jsonl`。体检、选品、方案全部来自 `task_orders.json / workshop_proposals.json`，与委托台无关。

结论：删掉 UI 不影响任何主链路；能力保留，等对话式助手来复用（见 Step 31 规划）。

## 2. 决策

| 选项 | 结论 | 理由 |
| --- | --- | --- |
| 连后端一起删（约 2100 行、219 条测试） | **不做** | 意图层是 12 意图 / 204 条语料评测 / LLM 消毒回退的成果，助手要直接复用；删了等于撤销一次架构决策 |
| 只摘前端 | **采用** | 改动 479 行前端 + 10 条静态对照测试，零风险，主链路不变 |
| 委托历史去处 | **完全隐藏** | 文件仍在 `data/tenants/<tid>/intake/history.jsonl`，只是没有 UI 入口 |

## 3. 改了什么

| 文件 | 改动 | 行数 |
| --- | --- | --- |
| `console/static/index.html` | 删导航项 `委托台`、`#intake-panel` 整段、侧栏 `#facts-panel` | 308 → 251 |
| `console/static/app.js` | 删 `resetIntake / submitIntake / renderIntake / renderUnderstood / renderPlan / renderOutcome`、facts 五个函数、对应常量与事件绑定、`loadOverview` 里的两次调用 | 1793 → 1371 |
| `console/static/styles.css` | 删 `.intake-*` 与 `.facts-*` 全部规则（含 720px 媒体查询） | 3365 → 2968 |
| `console/server.py` | 在 `/intake` 路由上方加注释：UI 已下线，接口保留，勿当死代码清理 | +3 |
| `tests/test_console_intake.py` | 删 `StaticAssetWiringTests`（5 条）；`IntakeAppTests.setUp` 强制 `INTAKE_ENGINE=rules` | -5 条 |
| `tests/test_memory_facts.py` | 删 `FactsStaticWiringTests`（5 条） | -5 条 |
| `README.md` | 测试数 503 → 889；补 Step 30 说明与文档索引 | +4 |

### 保留（一行未动）

- `intake/` 五个模块：`schema / router / llm_router / mapping / history`
- `console/app.py`：`intake_preview` / `intake_history` / `facts_*` / `plan_preview`
- `console/server.py`：`/api/tenants/{tid}/intake`、`/intake/history`、`/facts*` 路由
- 174 条意图层测试（router / llm / mapping / history / e2e）与 41 条委托台接口测试

## 4. 顺带修掉的一个隐患

`IntakeAppTests` 之前没锁引擎，走 `INTAKE_ENGINE=auto` 会真的调大模型：key 额度、限流、模型版本任一变化都会让断言漂移
（本次回归就出现「这个产品的价格和销量怎么样」被判成 `DATA_QUERY` 而不是 `CLARIFY`）。

现在 `setUp` 里 `mock.patch.dict(os.environ, {"INTAKE_ENGINE": "rules"})`，并与 `addCleanup` 配对。
接口测试本来就该测确定性行为，模型效果归 `scripts/eval_intake.py --engine llm` 管。

## 5. 验收

```bash
python -m unittest discover -s tests -q     # 889 OK (skipped=1)
node --check src/ops_agent/console/static/app.js
python run.py console serve --host 127.0.0.1 --port 8787
```

页面检查项：

- 左侧 7 项：总览 / 店铺体检 / 本周动作 / 方案与审批 / 运行记录 / 记忆 / 系统管理
- 首屏直接是 KPI + 店铺体检，无空白区
- 侧栏只剩「审批与发布」，底栏不再有事实输入框
- 浏览器控制台无 404、无 `Cannot read properties of null`
- 旧书签 `#intake-panel` 打开后落在页面顶部（锚点不存在，浏览器忽略）

## 6. 怎么恢复

UI 代码在 `git show <本步提交>^:src/ops_agent/console/static/index.html` 里，恢复顺序：

1. 从上一个提交取回 `index.html / app.js / styles.css`
2. 重新加回 `StaticAssetWiringTests` 与 `FactsStaticWiringTests`
3. 后端无需任何动作（一直在线）

如果恢复是为了做对话式助手，**不要**恢复表单式委托台，直接在 `intake/` 上加会话层。
