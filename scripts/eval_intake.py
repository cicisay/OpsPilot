"""评测：把「看着还行」变成几个可比的数字。

用法：
    python scripts/eval_intake.py            # 只跑意图（200 条）
    python scripts/eval_intake.py --full     # 意图 + 记忆 60 组 + 端到端 10 个任务 + 消融
    python scripts/eval_intake.py --top 10
    python scripts/eval_intake.py --no-report

口径（写死在代码里，避免每次口径漂移）：
- 意图准确率：`actual.intent == expected.intent`。复合委托额外要求 `also` 里的
  辅助意图出现在 `actual.candidates` 里，否则算不中——只报主意图等于放过一半。
- 槽位准确率：逐键比。只统计语料里写了期望槽位的样本；列表按集合比（顺序不算错）。
- 澄清触发率：期望 CLARIFY/UNKNOWN 的样本中，实际也给出 CLARIFY/UNKNOWN 的比例。
- 误执行率：期望 CLARIFY/UNKNOWN 的样本中，实际给出了具体业务意图的比例。
  这是安全指标——宁可多问一句，也不能把"帮我搞一下"当成批量上新跑掉。
- 记忆召回率：写入一条事实后，用相关问法检索，能召回同一条的比例。
- 冲突收敛率：新旧事实矛盾时，旧记录被标 superseded、检索只剩新记录的比例。
- 遗忘率：软删 / 过期 / 被替代的记录不再被召回的比例。
- 端到端通过率：按任务写死的不变量（不越过澄清、不落库、阻断数正确）逐条比。

脚本只读语料与 fixtures，只写一份评测报告（默认 `data/intake/`，该目录不进版本库）。
`data/` 下业务文件一个都不碰。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent import intake as intake_pkg  # noqa: E402
from ops_agent.intake import Intent, classify, explain  # noqa: E402
from ops_agent.memory.facts import FactsStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# 语料随代码走（tests/fixtures），不放 data/：data/ 是运行时状态，被 .gitignore
# 整体排除，干净 clone 上拿不到语料，评测就跑不起来。
DEFAULT_SET = ROOT / "tests" / "fixtures" / "intake" / "eval_set.jsonl"
# 报告反过来：它是运行产物，写进 data/（已忽略），不再弄脏工作区。
DEFAULT_REPORT = ROOT / "data" / "intake" / "eval_report.json"
FULL_REPORT = ROOT / "data" / "intake" / "eval_full_report.json"

# 这两类是"不该动手"的意图，误判成它们不算损失，误判成业务意图才算危险。
SAFE_INTENTS = {Intent.CLARIFY.value, Intent.UNKNOWN.value}


def load_set(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno} 不是合法 JSON: {exc}") from exc
    return rows


def _slot_match(expected, actual) -> bool:
    if isinstance(expected, list) or isinstance(actual, list):
        if not isinstance(expected, list) or not isinstance(actual, list):
            return False
        return set(expected) == set(actual)
    return expected == actual


def _classify_error(row: dict, result) -> tuple[str, str]:
    """给错例打一个可归因的标签 + 一句人话原因。"""
    expected = row["intent"]
    actual = result.intent.value
    hits = explain(row["text"])

    if expected in SAFE_INTENTS and actual not in SAFE_INTENTS:
        kind = "过度执行"
        reason = f"该反问/该拒答，却直接判成 {actual}（conf {result.confidence}）"
    elif expected not in SAFE_INTENTS and actual in SAFE_INTENTS:
        kind = "漏识别"
        reason = f"信息够但没接住，落到 {actual}（conf {result.confidence}）"
    else:
        kind = "判错类"
        reason = f"期望 {expected}，实际 {actual}（conf {result.confidence}）"

    if row.get("also"):
        missing_also = [item for item in row["also"] if item not in result.candidates]
        if missing_also:
            kind = "复合漏项"
            reason += f"；辅助意图 {missing_also} 没进 candidates"

    if hits:
        detail = "；".join(f"{name}←{'+'.join(labels)}" for name, labels in hits.items())
        reason += f"｜命中规则：{detail}"
    else:
        reason += "｜未命中任何规则"

    if result.missing:
        reason += f"｜缺槽位：{','.join(result.missing)}"
    return kind, reason


def evaluate(rows: list[dict]) -> dict:
    total = len(rows)
    intent_ok = 0
    slot_keys_total = 0
    slot_keys_ok = 0
    slot_rows_total = 0
    slot_rows_ok = 0
    safe_total = 0
    safe_clarified = 0
    errors: list[dict] = []
    per_intent: dict[str, dict] = {}

    for row in rows:
        expected = row["intent"]
        result = classify(row["text"])
        bucket = per_intent.setdefault(expected, {"total": 0, "ok": 0})
        bucket["total"] += 1

        hit = result.intent.value == expected
        if row.get("also"):
            hit = hit and all(item in result.candidates for item in row["also"])
        if hit:
            intent_ok += 1
            bucket["ok"] += 1

        expected_slots = row.get("slots") or {}
        if expected_slots:
            slot_rows_total += 1
            row_all_ok = True
            for name, expected_value in expected_slots.items():
                slot_keys_total += 1
                if name in result.slots and _slot_match(expected_value, result.slots[name]):
                    slot_keys_ok += 1
                else:
                    row_all_ok = False
            if row_all_ok:
                slot_rows_ok += 1

        if expected in SAFE_INTENTS:
            safe_total += 1
            if result.intent.value in SAFE_INTENTS:
                safe_clarified += 1

        if not hit:
            kind, reason = _classify_error(row, result)
            errors.append(
                {
                    "id": row["id"],
                    "text": row["text"],
                    "expected": expected,
                    "actual": result.intent.value,
                    "confidence": result.confidence,
                    "kind": kind,
                    "reason": reason,
                    "note": row.get("note", ""),
                }
            )

    metrics = {
        "total": total,
        "intent_accuracy": round(intent_ok / total, 4) if total else 0.0,
        "slot_key_accuracy": round(slot_keys_ok / slot_keys_total, 4) if slot_keys_total else 0.0,
        "slot_row_accuracy": round(slot_rows_ok / slot_rows_total, 4) if slot_rows_total else 0.0,
        "slot_rows": slot_rows_total,
        "clarify_recall": round(safe_clarified / safe_total, 4) if safe_total else 0.0,
        "false_execution_rate": round(1 - safe_clarified / safe_total, 4) if safe_total else 0.0,
        "safe_cases": safe_total,
        "error_count": len(errors),
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "metrics": metrics,
        "per_intent": per_intent,
        "error_kinds": dict(Counter(item["kind"] for item in errors).most_common()),
        "errors": errors,
    }


def print_table(report: dict, top: int) -> None:
    metrics = report["metrics"]
    print()
    print(f"{'指标':<16}{'数值':>10}  说明")
    print("-" * 66)
    print(f"{'意图准确率':<16}{metrics['intent_accuracy']:>10.2%}  总样本 {metrics['total']}")
    print(f"{'槽位键准确率':<14}{metrics['slot_key_accuracy']:>10.2%}  逐键比，共 {metrics['slot_rows']} 条带槽位期望")
    print(f"{'槽位整条全对':<14}{metrics['slot_row_accuracy']:>10.2%}  一条里所有期望槽位都对")
    print(f"{'澄清触发率':<15}{metrics['clarify_recall']:>10.2%}  {metrics['safe_cases']} 条该澄清/该拒答的样本")
    print(f"{'误执行率':<16}{metrics['false_execution_rate']:>10.2%}  越低越安全")

    print()
    print(f"{'意图':<22}{'命中/总数':>10}{'准确率':>9}")
    print("-" * 44)
    for name, bucket in sorted(report["per_intent"].items()):
        print(f"{name:<22}{bucket['ok']:>4}/{bucket['total']:<5}{bucket['ok'] / bucket['total']:>9.1%}")

    if report["error_kinds"]:
        print()
        print("错例分布：" + "，".join(f"{k} {v}" for k, v in report["error_kinds"].items()))

    if report["errors"]:
        print()
        print(f"错例 TOP {min(top, len(report['errors']))}：")
        for item in report["errors"][:top]:
            print(f"  [{item['id']}] {item['text']}")
            print(f"        期望 {item['expected']} / 实际 {item['actual']} · {item['kind']}")
            print(f"        {item['reason']}")
    print()


# ---------------------------------------------------------------------------
# Step 8：记忆评测
# ---------------------------------------------------------------------------

MEMORY_RECALL_GROUPS = 30
MEMORY_CONFLICT_GROUPS = 15
MEMORY_FORGET_GROUPS = 15
FORGET_KINDS = ("revoked", "expired", "superseded")


def _memory_failure(group: str, index: int, reason: str, **detail) -> dict:
    return {"group": group, "index": index, "reason": reason, **detail}


def evaluate_memory(now: datetime | None = None) -> dict:
    """三组记忆行为：该想起、该收敛、该忘。

    每组用独立的临时目录，互不干扰；跑完随 `TemporaryDirectory` 删除，
    不在 `data/` 留任何东西。
    """

    clock = (now or datetime.now()).replace(microsecond=0)
    failures: list[dict] = []
    recall_ok = 0
    conflict_ok = 0
    forget_ok = 0
    forget_by_kind: dict[str, dict] = {kind: {"total": 0, "ok": 0} for kind in FORGET_KINDS}

    with tempfile.TemporaryDirectory(prefix="eval-memory-") as tmp:
        root = Path(tmp)

        # --- 30 组：该想起 -------------------------------------------------
        for index in range(MEMORY_RECALL_GROUPS):
            store = FactsStore(root / f"recall-{index:02d}")
            content = f"美国站类目A{index:02d}需要提供认证材料"
            added = store.add(
                scope="shop",
                fact_type="fact",
                content=content,
                source="user_instruction",
                importance=0.8,
                now=clock,
            )
            memory_id = added["fact"]["memory_id"]
            found = store.search(query=f"类目A{index:02d}认证", budget_tokens=800, now=clock)
            hit = memory_id in {item["memory_id"] for item in found["facts"]}
            if hit:
                recall_ok += 1
            else:
                failures.append(
                    _memory_failure("recall", index, "写入后检索不到", memory_id=memory_id, query=f"类目A{index:02d}认证")
                )

        # --- 15 组：冲突 ---------------------------------------------------
        for index in range(MEMORY_CONFLICT_GROUPS):
            store = FactsStore(root / f"conflict-{index:02d}")
            old = store.add(
                scope="shop",
                fact_type="rule",
                content=f"不做类目B{index:02d}：利润太薄",
                source="user_instruction",
                now=clock,
            )["fact"]
            new = store.add(
                scope="shop",
                fact_type="rule",
                content=f"可以做类目B{index:02d}：找到供应商了",
                source="user_instruction",
                now=clock,
            )
            old_now = store.get(old["memory_id"], now=clock) or {}
            found = store.search(query=f"类目B{index:02d}", budget_tokens=800, now=clock)
            ids = {item["memory_id"] for item in found["facts"]}
            ok = (
                old_now.get("effective_status") == "superseded"
                and old["memory_id"] not in ids
                and new["fact"]["memory_id"] in ids
                and new["superseded"] == [old["memory_id"]]
            )
            if ok:
                conflict_ok += 1
            else:
                failures.append(
                    _memory_failure(
                        "conflict",
                        index,
                        "冲突没有收敛成“旧的失效、新的生效”",
                        old_status=old_now.get("effective_status"),
                        superseded=new["superseded"],
                        recalled=sorted(ids),
                    )
                )

        # --- 15 组：该忘（软删 / 过期 / 被替代 各 5 组） ---------------------
        per_kind = MEMORY_FORGET_GROUPS // len(FORGET_KINDS)
        for kind in FORGET_KINDS:
            for index in range(per_kind):
                store = FactsStore(root / f"forget-{kind}-{index:02d}")
                content = f"类目C{kind}{index:02d}的旧规则" if kind != "superseded" else f"不做类目C{kind}{index:02d}：利润太薄"
                created = clock - timedelta(days=200) if kind == "expired" else clock
                added = store.add(
                    scope="shop",
                    fact_type="rule",
                    content=content,
                    source="user_instruction",
                    importance=0.2 if kind == "expired" else 0.8,
                    now=created,
                )["fact"]
                memory_id = added["memory_id"]

                if kind == "revoked":
                    store.revoke(memory_id, reason="评测：软删", now=clock)
                elif kind == "superseded":
                    store.add(
                        scope="shop",
                        fact_type="rule",
                        content=f"可以做类目C{kind}{index:02d}：找到供应商了",
                        source="user_instruction",
                        now=clock,
                    )
                # expired 不需要动作：状态由 created_at + importance 推导，验证的是
                # "不依赖后台任务也不会召回"。

                found = store.search(query=content, budget_tokens=800, now=clock)
                ids = {item["memory_id"] for item in found["facts"]}
                forget_by_kind[kind]["total"] += 1
                if memory_id not in ids:
                    forget_ok += 1
                    forget_by_kind[kind]["ok"] += 1
                else:
                    failures.append(
                        _memory_failure("forget", index, f"{kind} 的记录仍被召回", kind=kind, memory_id=memory_id)
                    )

    recall_rate = recall_ok / MEMORY_RECALL_GROUPS
    conflict_rate = conflict_ok / MEMORY_CONFLICT_GROUPS
    forget_total = MEMORY_FORGET_GROUPS
    forget_rate = forget_ok / forget_total
    total = MEMORY_RECALL_GROUPS + MEMORY_CONFLICT_GROUPS + forget_total
    passed = recall_ok + conflict_ok + forget_ok
    return {
        "groups": total,
        "passed": passed,
        "metrics": {
            "recall_rate": round(recall_rate, 4),
            "conflict_rate": round(conflict_rate, 4),
            "forget_rate": round(forget_rate, 4),
            "overall": round(passed / total, 4),
        },
        "breakdown": {
            "recall": {"total": MEMORY_RECALL_GROUPS, "ok": recall_ok},
            "conflict": {"total": MEMORY_CONFLICT_GROUPS, "ok": conflict_ok},
            "forget": {"total": forget_total, "ok": forget_ok, "by_kind": forget_by_kind},
        },
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Step 8：端到端评测
# ---------------------------------------------------------------------------

#: 10 个任务。`expect` 只写**不变量**（安全 + 阻断），不写"希望它多聪明"：
#: 期望写得比实现聪明，评测就会变成自我安慰。做不到的地方放 `quality`，单独统计。
E2E_TASKS: list[dict] = [
    {
        "id": "E2E-01",
        "text": "这 20 个新品上到美国站",
        "expect": {"status": "ok", "drafts": 3, "blocked": 1, "orders_min": 1, "clarify_min": 1},
    },
    {
        "id": "E2E-02",
        "text": "上这 3 个新品到美国站",
        "expect": {"status": "ok", "drafts": 3, "blocked": 1, "orders_min": 1},
    },
    {
        "id": "E2E-03",
        "text": "上 5 个新品到德国站",
        "expect": {"status": "ok", "drafts": 3, "blocked": 1},
        "quality": "资料市场写死 US，委托是 DE；草稿已标 market_conflict → needs_fill，等人确认",
    },
    {
        "id": "E2E-04",
        "text": "把 FT-001 的标题重写一下",
        "expect": {"status": "ok", "orders": 1, "drafts": 0},
    },
    {
        "id": "E2E-05",
        "text": "这款要不要上",
        "expect": {"status": "clarify", "orders": 0, "drafts": 0},
        "quality": "“这款”没有指代，问 sku 是正确行为",
    },
    {
        "id": "E2E-06",
        "text": "帮我搞一下",
        "expect": {"status": "clarify", "orders": 0, "drafts": 0},
    },
    {
        "id": "E2E-07",
        "text": "帮我弄弄这个标题和图片",
        "expect": {"status": "clarify", "orders": 0, "drafts": 0},
    },
    {
        "id": "E2E-08",
        "text": "把商品翻译成德语和法语",
        "expect": {"status": "clarify", "orders": 0, "drafts": 0},
    },
    {
        "id": "E2E-09",
        "text": "帮我看看最近 7 天的曝光和点击",
        "expect": {"status": "ok", "orders": 0, "drafts": 0},
        "quality": "产出物是 answer，不派单是正确的；本步不实现问答渲染",
    },
    {
        "id": "E2E-10",
        "text": "这批货怎么样",
        "expect": {"status": "clarify", "orders": 0, "drafts": 0},
    },
]

E2E_KEYS = ("status", "orders", "drafts", "blocked", "clarify", "ready", "needs_fill", "steps")


def _demo_module():
    """按文件路径加载 demo_intake，保证评测与演示跑的是同一段代码。"""

    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import demo_intake  # noqa: PLC0415

    return demo_intake


def _e2e_actual(trace: dict) -> dict:
    summary = trace.get("summary") or {}
    return {
        "status": trace.get("status"),
        "orders": len(trace.get("orders") or []),
        "drafts": len(trace.get("drafts") or []),
        "blocked": summary.get("blocked", 0),
        "clarify": len(trace.get("clarifications") or []),
        "ready": summary.get("ready", 0),
        "needs_fill": summary.get("needs_fill", 0),
        "steps": len(trace.get("stages") or []),
    }


def _expect_ok(expected: dict, actual: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    for key, value in expected.items():
        if key == "orders_min":
            if actual["orders"] < value:
                problems.append(f"orders {actual['orders']} < {value}")
        elif key == "clarify_min":
            if actual["clarify"] < value:
                problems.append(f"clarify {actual['clarify']} < {value}")
        elif actual.get(key) != value:
            problems.append(f"{key}={actual.get(key)!r}，期望 {value!r}")
    return (not problems), problems


def evaluate_e2e(tasks: list[dict] | None = None, *, memory: bool = True) -> dict:
    demo = _demo_module()
    rows: list[dict] = []
    failures: list[dict] = []
    quality: list[dict] = []
    passed = 0

    for task in tasks or E2E_TASKS:
        trace = demo.run_pipeline(task["text"], memory=memory)
        actual = _e2e_actual(trace)
        ok, problems = _expect_ok(task["expect"], actual)
        passed += 1 if ok else 0
        row = {
            "id": task["id"],
            "text": task["text"],
            "intent": (trace.get("intent") or {}).get("name"),
            "expect": task["expect"],
            "actual": actual,
            "ok": ok,
            "warnings": list(trace.get("warnings") or []),
        }
        if task.get("quality"):
            row["quality"] = task["quality"]
            quality.append({"id": task["id"], "text": task["text"], "note": task["quality"]})
        rows.append(row)
        if not ok:
            failures.append({"id": task["id"], "problems": problems, "actual": actual})

    total = len(rows)
    return {
        "tasks": total,
        "passed": passed,
        "metrics": {
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "blocked_total": sum(row["actual"]["blocked"] for row in rows),
            "clarify_total": sum(row["actual"]["clarify"] for row in rows),
        },
        "rows": rows,
        "failures": failures,
        "quality_findings": quality,
    }


# ---------------------------------------------------------------------------
# Step 8：消融
# ---------------------------------------------------------------------------


def _aggregate(traces: list[dict]) -> dict:
    drafts = sum(len(trace.get("drafts") or []) for trace in traces)
    review = sum((trace.get("summary") or {}).get("review_points", 0) for trace in traces)
    return {
        "tasks": len(traces),
        "steps_total": sum(len(trace.get("stages") or []) for trace in traces),
        "steps_mean": round(sum(len(trace.get("stages") or []) for trace in traces) / max(1, len(traces)), 2),
        "drafts": drafts,
        "review_points": review,
        # 代理指标：必须有人看一眼的位置 / 草稿数。不是真实编辑次数，口径见 README。
        "edit_rate": round(review / max(1, drafts), 4),
    }


def evaluate_ablations(tasks: list[dict] | None = None) -> dict:
    """两组消融：计划预览、作业记忆。

    这组对比只改一个开关，任务集、CSV、记忆库都不动。结论要按实测写：
    预览不改变"要人处理多少处"，它改变的是这些点**提不提前看得见**；
    记忆不改变草稿状态，它改变的是草稿里带不带上下文。
    """

    demo = _demo_module()
    tasks = tasks or E2E_TASKS

    plan_arms: list[dict] = []
    for preview in (True, False):
        traces = [demo.run_pipeline(task["text"], plan_preview=preview) for task in tasks]
        arm = _aggregate(traces)
        assumed = sum(len((trace.get("plan") or {}).get("assumed") or {}) for trace in traces)
        arm.update(
            {
                "arm": "with_preview" if preview else "no_preview",
                "preview": preview,
                "visible_assumptions": assumed if preview else 0,
                "hidden_assumptions": 0 if preview else assumed,
            }
        )
        plan_arms.append(arm)

    memory_arms: list[dict] = []
    for enabled in (True, False):
        traces = [demo.run_pipeline(task["text"], memory=enabled) for task in tasks]
        arm = _aggregate(traces)
        arm.update(
            {
                "arm": "with_memory" if enabled else "no_memory",
                "memory": enabled,
                "facts_injected": sum((trace.get("memory") or {}).get("count", 0) for trace in traces),
                "memory_tokens": sum((trace.get("memory") or {}).get("tokens", 0) for trace in traces),
                "drafts_with_facts": sum(
                    1
                    for trace in traces
                    for draft in trace.get("drafts") or []
                    if draft.get("facts_used")
                ),
            }
        )
        memory_arms.append(arm)

    return {"plan_preview": plan_arms, "memory": memory_arms}


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------


def print_memory(report: dict) -> None:
    metrics = report["metrics"]
    breakdown = report["breakdown"]
    print()
    print(f"{'记忆评测':<18}{'数值':>10}  说明")
    print("-" * 66)
    print(f"{'召回率':<18}{metrics['recall_rate']:>10.2%}  {breakdown['recall']['ok']}/{breakdown['recall']['total']} 该想起")
    print(f"{'冲突收敛率':<16}{metrics['conflict_rate']:>10.2%}  {breakdown['conflict']['ok']}/{breakdown['conflict']['total']} 冲突后旧的失效")
    print(f"{'遗忘率':<18}{metrics['forget_rate']:>10.2%}  {breakdown['forget']['ok']}/{breakdown['forget']['total']} 软删/过期/被替代")
    print(f"{'总体':<19}{metrics['overall']:>10.2%}  共 {report['groups']} 组")
    for item in report["failures"][:5]:
        print(f"  失败 [{item['group']}#{item['index']}] {item['reason']}")


def print_e2e(report: dict) -> None:
    print()
    print(f"{'端到端':<10}{'任务':<38}{'结果':>6}  草稿/阻断/澄清")
    print("-" * 78)
    for row in report["rows"]:
        actual = row["actual"]
        mark = "通过" if row["ok"] else "不通过"
        print(
            f"{row['id']:<10}{row['text'][:34]:<38}{mark:>6}"
            f"  {actual['drafts']}/{actual['blocked']}/{actual['clarify']}"
        )
    print(f"通过率 {report['metrics']['pass_rate']:.0%}（{report['passed']}/{report['tasks']}）")
    if report["quality_findings"]:
        print("质量备注（不影响通过判定，但要看）：")
        for item in report["quality_findings"]:
            print(f"  · {item['id']} {item['note']}")


def print_ablations(report: dict) -> None:
    print()
    print("消融对比")
    print("-" * 78)
    print(f"{'实验':<16}{'任务':>5}{'阶段合计':>9}{'阶段均值':>9}{'草稿':>5}{'人处理点':>9}{'修改率':>8}")
    for arm in report["plan_preview"]:
        print(
            f"{arm['arm']:<16}{arm['tasks']:>5}{arm['steps_total']:>9}{arm['steps_mean']:>9}"
            f"{arm['drafts']:>5}{arm['review_points']:>9}{arm['edit_rate']:>8.2f}"
        )
    for arm in report["plan_preview"]:
        print(f"  {arm['arm']}：可见假设 {arm['visible_assumptions']} 条，隐性假设 {arm['hidden_assumptions']} 条")
    print()
    for arm in report["memory"]:
        print(
            f"{arm['arm']:<16}{arm['tasks']:>5}{arm['steps_total']:>9}{arm['steps_mean']:>9}"
            f"{arm['drafts']:>5}{arm['review_points']:>9}{arm['edit_rate']:>8.2f}"
            f"  注入事实 {arm['facts_injected']} 条 / {arm['memory_tokens']} tokens"
        )
    print()


#: Step 8 期间实测到、但不在本步改动范围内的缺陷。写进报告，不藏。
#: 已知问题。已修的不删，改成 status=closed 并写清修法——删掉就等于把"这里踩过坑"
#: 这件事也删了，下次还会踩。
KNOWN_ISSUES: list[dict] = [
    {
        "id": "K-01",
        "severity": "中",
        "area": "intake/router.py",
        "status": "closed",
        "desc": "sku 只识别 SKU-数字，抽不到自有编码（FT-001）",
        "fix": "新增 _SKU_CODE（2-6 字母 + 连字符/下划线 + 2-6 数字），先认 SKU-数字再认自有编码",
    },
    {
        "id": "K-02",
        "severity": "中",
        "area": "ingest.py",
        "status": "closed",
        "desc": "委托市场与资料 market 冲突时静默取资料值",
        "fix": "冲突时 missing+market_conflict → needs_fill，notes 写明两个市场",
    },
    {
        "id": "K-03",
        "severity": "低",
        "area": "scripts/demo_intake.py",
        "status": "open",
        "desc": "记忆只作上下文注入，尚未参与槽位补全；消融显示记忆不改变草稿状态",
    },
    {
        "id": "K-04",
        "severity": "低",
        "area": "intake/router.py",
        "status": "closed",
        "desc": "“帮我弄弄这个标题和图片”被判 MEDIA_COMPOSE（0.69），靠缺 sku 才停下",
        "fix": "模糊动词 + 文案/素材两个部位并列 → CLARIFY；放在复合判定之后，避免抢走有连接词的主任务",
    },
]



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="委托链路评测（意图 / 记忆 / 端到端）")
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, help="评测语料 jsonl")
    parser.add_argument("--report", type=Path, default=None, help="报告落盘路径（默认 data/intake/）")
    parser.add_argument("--top", type=int, default=5, help="打印前 N 条错例")
    parser.add_argument("--full", action="store_true", help="跑意图 + 记忆 + 端到端 + 消融")
    parser.add_argument("--no-report", action="store_true", help="只打印，不落盘")
    parser.add_argument(
        "--engine",
        choices=["rules", "llm"],
        default="rules",
        help="意图引擎：llm 会对每条语料真实调用大模型（204 次请求，需可用额度）",
    )
    args = parser.parse_args(argv)

    if not args.set.exists():
        raise SystemExit(f"语料不存在: {args.set}")

    if args.engine == "llm":
        # 模块级重绑：evaluate 内部引用的是本模块的 classify 名字。
        global classify
        classify = intake_pkg.classify_auto

    rows = load_set(args.set)
    intent_report = evaluate(rows)
    intent_report["set"] = str(args.set.relative_to(ROOT) if args.set.is_relative_to(ROOT) else args.set)
    intent_report["engine"] = args.engine
    print_table(intent_report, args.top)

    if args.full:
        memory_report = evaluate_memory()
        print_memory(memory_report)
        e2e_report = evaluate_e2e()
        print_e2e(e2e_report)
        ablations = evaluate_ablations()
        print_ablations(ablations)
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "set": intent_report["set"],
            "intent": intent_report,
            # 顶层再放一份 metrics：旧消费者读 report["metrics"] 不用改。
            "metrics": intent_report["metrics"],
            "memory": memory_report,
            "e2e": e2e_report,
            "ablations": ablations,
            "known_issues": KNOWN_ISSUES,
        }
        target = args.report or FULL_REPORT
    else:
        report = intent_report
        target = args.report or DEFAULT_REPORT

    if not args.no_report:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告已写入 {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
