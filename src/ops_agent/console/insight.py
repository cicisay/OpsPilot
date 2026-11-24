"""Read-only view models for the ops console.

Every panel needs the same few files joined together, and the console's HTTP
layer must never do that joining while holding a request. This module is the
single place where tenant files become panel data: it only reads, never writes,
and it degrades to an explicit ``available: false`` instead of raising when a
file is missing or half-written.

The one rule worth stating: nothing here recomputes a verdict. Compliance
severity, experiment verdicts and quota decisions are decided upstream by
``compliance.py`` / ``experiment.py`` / ``quota.py``. The console's job is to
show them faithfully, so a disagreement between two panels is a bug in the
producer, not something this module is allowed to paper over.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from ..experiment import is_due, parse_day
from ..memory import MemoryStore

CHECK_KEYS = ("c1_absolute", "c2_ip", "c3_category", "c4_market")
CHECK_LABELS = {
    "c1_absolute": "绝对化与承诺",
    "c2_ip": "品牌与知识产权",
    "c3_category": "禁售类目与资质",
    "c4_market": "目标市场法规",
}
SEVERITY_CN = {"pass": "通过", "warn": "提示", "block": "否决"}
VERDICT_CN = {
    "supported": "成立",
    "rejected": "不成立",
    "inconclusive": "不确定",
    "insufficient": "样本不足",
}
# ``run_id`` doubles as a filename when replaying a snapshot, so it is matched
# against the only shape the recorder ever writes.
RUN_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")

# Thresholds come from PRD 12.1; the console only reports against them.
HEALTH_TARGETS = {
    "completion": (0.8, "lower"),
    "block_rate": (0.3, "higher"),
    "code_override": (0, "higher"),
    "quota_trim": (0.2, "higher"),
    "verdict_rate": (0.9, "lower"),
    "calls_per_item": (5, "higher"),
}


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _text(value, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _int(value, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _rate(value, digits: int = 3):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), digits)


def _rate6(value):
    """Rates live at 0.0001 scale; three decimals would round them all to 0.0."""
    return _rate(value, 6)


# --------------------------------------------------------------- 体检面板


def _freshness(orders: dict) -> dict:
    """任务单里的数据日期，翻成运营台能直接显示的等级。

    体检分看着像一个"当下状态"，其实是一批历史数字算出来的。把数据日期
    摆到面板上，才不会出现"拿三个月前的照片当本周体检"。
    """
    raw = _text(orders.get("data_as_of"), 20)
    source = _text(orders.get("data_source"), 20)
    day = None
    if raw:
        try:
            day = date.fromisoformat(raw[:10])
        except ValueError:
            day = None
    if day is None:
        return {
            "available": False,
            "as_of": raw,
            "age_days": None,
            "level": "unknown",
            "label": "数据日期未知（旧任务单，建议重跑）",
            "source": source,
        }
    age = max(0, (date.today() - day).days)
    level = "fresh" if age <= 7 else "warn" if age <= 30 else "danger"
    if age == 0:
        label = "数据为今天（实时读取）" if source == "live" else "数据为今天"
    else:
        label = f"数据截至 {age} 天前"
    return {
        "available": True,
        "as_of": day.isoformat(),
        "age_days": age,
        "level": level,
        "label": label,
        "source": source,
    }


def build_checkup(directory: Path) -> dict:
    """Store health, quota decision and where the anomalies cluster.

    The category breakdown is deliberately built from *selected* tasks rather
    than the whole catalogue: an operator opens this panel to answer "what is
    being touched this week and why", and the full-store weak list is already
    stated in the health summary.
    """
    orders = _read_json(Path(directory) / "task_orders.json", {})
    if not isinstance(orders, dict):
        orders = {}
    health = _mapping(orders.get("store_health"))
    quota = _mapping(orders.get("quota"))
    selected = _list(orders.get("selected"))

    categories: dict[str, dict] = {}
    for task in selected:
        key = _text(task.get("category"), 60) or "未分类"
        entry = categories.setdefault(
            key, {"category": key, "count": 0, "problems": {}, "product_ids": []}
        )
        entry["count"] += 1
        entry["product_ids"].append(_text(task.get("product_id"), 60))
        problem = _text(task.get("problem"), 60)
        label = _text(task.get("problem_label"), 60) or problem
        if problem or label:
            bucket = entry["problems"].setdefault(
                problem or label, {"problem": problem, "label": label, "count": 0}
            )
            bucket["count"] += 1

    weak = [_text(item, 60) for item in _list(health.get("weak_categories")) if _text(item, 60)]
    for name in weak:
        categories.setdefault(
            name, {"category": name, "count": 0, "problems": {}, "product_ids": []}
        )
    breakdown = []
    for entry in categories.values():
        problems = sorted(entry["problems"].values(), key=lambda item: -item["count"])
        breakdown.append(
            {
                "category": entry["category"],
                "count": entry["count"],
                "weak": entry["category"] in weak,
                "problems": problems[:4],
                "product_ids": entry["product_ids"][:12],
            }
        )
    breakdown.sort(key=lambda item: (-item["count"], item["category"]))

    notes = [_text(item, 240) for item in _list(orders.get("notes")) if _text(item, 240)]
    return {
        "available": bool(health) or bool(selected),
        "run_id": _text(orders.get("run_id"), 64),
        "generated_at": _text(orders.get("generated_at"), 40),
        "freshness": _freshness(orders),
        "planner": _text(orders.get("planner"), 40),
        "model": _text(orders.get("model"), 60),
        "scanned": _int(orders.get("scanned")),
        "matched": _int(orders.get("matched")),
        "health": {
            "score": _int(health.get("score"), -1),
            "summary": _text(health.get("summary"), 900),
            "top_problem": _text(health.get("top_problem"), 240),
            "weak_categories": weak,
        },
        "quota": {
            "value": _int(quota.get("value")),
            "low": _int(quota.get("low")),
            "high": _int(quota.get("high")),
            "requested": _int(quota.get("requested")),
            "guarded": bool(quota.get("guarded")),
            "underfill": bool(quota.get("underfill")),
            "underfill_reason": _text(
                quota.get("underfill_reason") or orders.get("underfill_reason"), 240
            ),
            "reason": _text(quota.get("reason"), 120),
        },
        "categories": breakdown,
        "notes": notes[:6],
        "reasons": [
            _text(task.get("variant_reason"), 300)
            for task in selected[:6]
            if _text(task.get("variant_reason"), 300)
        ],
    }


# --------------------------------------------------------------- 实验看板


def _metric_value(block: dict, metric: str):
    """Pick the number the experiment is actually judged on."""
    if not isinstance(block, dict):
        return None, ""
    key = metric if metric in ("order_rate", "click_rate", "return_rate") else "order_rate"
    return _rate6(block.get(key)), key


def build_experiment_board(directory: Path, today=None) -> dict:
    """Observing vs judged, with the numbers an operator needs to trust them.

    ``days_left`` is recomputed from ``started_at`` instead of reusing the value
    stored in ``outcome.window``: that value was correct when the judgement ran
    and is stale by definition afterwards.
    """
    today = today or date.today()
    if isinstance(today, str):
        today = parse_day(today) or date.today()
    store = MemoryStore(Path(directory) / "memory")
    records = store.read_experiments(limit=500, include_open=True)

    observing: list[dict] = []
    judged: list[dict] = []
    for record in records:
        start = parse_day(record.get("started_at"))
        window = _int(record.get("window_days"), 14) or 14
        ends = start + timedelta(days=window) if start else None
        days_left = max(0, (ends - today).days) if ends else None
        overdue = bool(ends and today > ends)
        outcome = _mapping(record.get("outcome"))
        metric = _text(record.get("metric"), 40) or "order_rate"
        base_value, base_key = _metric_value(_mapping(record.get("baseline")), metric)
        current_value, _ = _metric_value(_mapping(outcome.get("treated")), metric)
        sample = _mapping(outcome.get("sample"))
        verdict = _text(record.get("verdict"), 40)
        lift = outcome.get("lift")
        item = {
            "experiment_id": _text(record.get("experiment_id"), 120),
            "product_id": _text(record.get("product_id"), 60),
            "title": _text(record.get("title"), 120) or _text(record.get("product_id"), 60),
            "variant_key": _text(record.get("variant_key"), 80),
            "angle": _text(record.get("angle"), 80),
            "metric": base_key,
            "metric_label": {"order_rate": "订单率", "click_rate": "点击率", "return_rate": "退货率"}.get(
                base_key, base_key
            ),
            "baseline": base_value,
            "current": current_value,
            "min_lift": _rate(record.get("min_lift"), 4),
            "lift": _rate(lift, 4) if lift is not None else None,
            "p_value": _rate(outcome.get("p_value"), 6),
            "status": _text(record.get("status"), 20) or "observing",
            "verdict": verdict,
            "verdict_cn": _text(record.get("verdict_cn"), 20) or VERDICT_CN.get(verdict, ""),
            "started_at": _text(record.get("started_at"), 20),
            "ends_at": ends.isoformat() if ends else "",
            "window_days": window,
            "days_left": days_left,
            "overdue": overdue,
            "sample": {
                "days": _int(sample.get("days")),
                "impressions": _int(sample.get("impressions")),
                "clicks": _int(sample.get("clicks")),
                "orders": _int(sample.get("orders")),
            },
            "required_impressions": _int(outcome.get("required_impressions")) or None,
            "note": _text(outcome.get("note"), 300),
            "hypothesis": _text(record.get("hypothesis"), 400),
            "success_metric": _text(record.get("success_metric"), 200),
        }
        if verdict:
            judged.append(item)
        else:
            observing.append(item)

    judged.sort(key=lambda item: item["started_at"], reverse=True)
    observing.sort(key=lambda item: (item["days_left"] if item["days_left"] is not None else 999))

    counts = {"supported": 0, "rejected": 0, "inconclusive": 0, "insufficient": 0}
    for item in judged:
        if item["verdict"] in counts:
            counts[item["verdict"]] += 1
    return {
        "observing": observing[:30],
        "judged": judged[:30],
        "summary": {
            "total": len(records),
            "observing": len(observing),
            "judged": len(judged),
            **counts,
        },
    }


# --------------------------------------------------------------- 记忆浏览


def _snapshot_index(directory: Path) -> list[dict]:
    folder = Path(directory) / "memory" / "snapshots"
    items: list[dict] = []
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return items
    for path in paths[-20:]:
        payload = _read_json(path, {})
        if not isinstance(payload, dict):
            continue
        items.append(
            {
                "run_id": _text(payload.get("run_id") or path.stem, 64),
                "saved_at": _text(payload.get("saved_at"), 40),
                "experiments": len(_list(payload.get("experiments"))),
                "decisions": len(_list(payload.get("decisions"))),
                "lessons": len(_list(payload.get("lessons"))),
                "has_profile": bool(payload.get("store_profile")),
            }
        )
    items.sort(key=lambda item: item["saved_at"], reverse=True)
    return items


def build_memory(directory: Path) -> dict:
    """Read-only memory browser: profile, recent decisions, lessons, snapshots."""
    root = Path(directory) / "memory"
    store = MemoryStore(root)
    profile = store.read_profile()
    decisions = store.read_decisions(limit=20)
    lessons = store.read_lessons()
    health = store.health()
    return {
        "profile": profile,
        "decisions": decisions,
        "lessons": lessons,
        "health": health,
        "snapshots": _snapshot_index(directory),
    }


def replay_snapshot(directory: Path, run_id: str) -> dict:
    """Return the exact memory a run was given.

    ``run_id`` becomes a filesystem path segment, so anything that is not a
    plain recorder-style id is refused rather than sanitised: silently
    rewriting a traversal attempt would hide an attack from the audit log.
    """
    run_id = str(run_id or "").strip()
    if not RUN_ID_RE.match(run_id):
        raise ValueError("run_id 格式不合法")
    folder = Path(directory) / "memory" / "snapshots"
    path = folder / f"{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(run_id)
    payload = _read_json(path, {})
    if not isinstance(payload, dict):
        raise ValueError("快照文件不可解析")
    return {
        "run_id": _text(payload.get("run_id") or run_id, 64),
        "saved_at": _text(payload.get("saved_at"), 40),
        "store_profile": _mapping(payload.get("store_profile")),
        "experiments": _list(payload.get("experiments")),
        "decisions": _list(payload.get("decisions")),
        "lessons": _list(payload.get("lessons")),
    }


# --------------------------------------------------------------- 合规矩阵


def attach_verdicts(proposals: list[dict], reviews: list[dict]) -> list[dict]:
    """Join compliance verdicts onto each variant, four checks per version.

    Both spellings of the variant id are indexed because the review writer
    accepts either; a variant with no verdict keeps ``checked: false`` so the
    UI can say "未审" instead of implying a pass.
    """
    index: dict[tuple[str, str], dict] = {}
    for item in reviews:
        if not isinstance(item, dict):
            continue
        product_id = _text(item.get("product_id"), 60)
        for key in (_text(item.get("variant_key"), 80), _text(item.get("variant_id"), 80)):
            if key:
                index.setdefault((product_id, key), item)

    output: list[dict] = []
    for proposal in proposals:
        proposal = dict(proposal) if isinstance(proposal, dict) else {}
        product_id = _text(proposal.get("product_id"), 60)
        variants = []
        for variant in _list(proposal.get("variants")):
            variant = dict(variant) if isinstance(variant, dict) else {}
            key = _text(variant.get("variant_key") or variant.get("variant_id"), 80)
            verdict = index.get((product_id, key)) or {}
            checks = _mapping(verdict.get("checks"))
            normalized = {name: _text(checks.get(name), 10) or "missing" for name in CHECK_KEYS}
            findings = []
            for finding in _list(verdict.get("findings"))[:6]:
                if not isinstance(finding, dict):
                    continue
                findings.append(
                    {
                        "category": _text(finding.get("category"), 20),
                        "quote": _text(finding.get("quote"), 240),
                        "rule": _text(finding.get("rule"), 500),
                        "suggestion": _text(finding.get("suggestion"), 300),
                    }
                )
            severity = _text(verdict.get("severity"), 10)
            variant["compliance"] = {
                "checked": bool(verdict),
                "checks": normalized,
                "severity": severity,
                "label": SEVERITY_CN.get(severity, "未审"),
                "findings": findings,
                "overridden_by": _text(verdict.get("overridden_by"), 20),
                "headline": _text(verdict.get("headline"), 400),
            }
            # A variant is approvable only when the reviewer actually said so.
            # Missing verdicts and blocks both keep it out of the queue.
            variant["approvable"] = bool(verdict) and severity != "block"
            variants.append(variant)
        proposal["variants"] = variants
        proposal["compliance_summary"] = _summarize_compliance(variants)
        output.append(proposal)
    return output


def _summarize_compliance(variants: list[dict]) -> dict:
    counts = {"pass": 0, "warn": 0, "block": 0, "missing": 0}
    for variant in variants:
        severity = _mapping(variant.get("compliance")).get("severity") or "missing"
        counts[severity] = counts.get(severity, 0) + 1
    return {
        "total": len(variants),
        "approvable": sum(1 for item in variants if item.get("approvable")),
        **counts,
    }


# --------------------------------------------------------------- 运营指标

def _trace_files(trace_dir: Path | None) -> list[Path]:
    if not trace_dir:
        return []
    try:
        return sorted(Path(trace_dir).glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return []


def _calls_from_trace(trace_dir: Path | None) -> dict:
    """Call counts per agent from the newest trace file.

    Traces are per-run, not per-tenant, so this is a whole-machine view. It is
    reported as "最近一轮" and never mixed into the tenant's own numbers.
    """
    files = _trace_files(trace_dir)
    if not files:
        return {"run_id": "", "calls": 0, "failures": 0, "by_agent": {}}
    path = files[-1]
    by_agent: dict[str, dict] = {}
    failures = 0
    calls = 0
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return {"run_id": path.stem, "calls": 0, "failures": 0, "by_agent": {}}
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            agent = _text(entry.get("agent"), 40) or "unknown"
            bucket = by_agent.setdefault(agent, {"calls": 0, "failures": 0, "tokens": 0})
            bucket["calls"] += 1
            bucket["tokens"] += _int(entry.get("prompt_tokens")) + _int(entry.get("completion_tokens"))
            calls += 1
            if not entry.get("ok", True):
                bucket["failures"] += 1
                failures += 1
    return {"run_id": path.stem, "calls": calls, "failures": failures, "by_agent": by_agent}


def build_metrics(directory: Path, today=None, trace_dir: Path | None = None) -> dict:
    """The six PRD 12.1 indicators, each with its own alert threshold."""
    directory = Path(directory)
    orders = _read_json(directory / "task_orders.json", {})
    proposals = _list(_read_json(directory / "workshop_proposals.json", {}).get("proposals"))
    reviews = _list(_read_json(directory / "review_results.json", {}).get("results"))
    store = MemoryStore(directory / "memory")

    planned = 0
    for task in _list(orders.get("selected") if isinstance(orders, dict) else []):
        planned += _int(task.get("variant_max"))
    produced = len(reviews) or sum(len(_list(item.get("variants"))) for item in proposals)

    severity_counts = {"pass": 0, "warn": 0, "block": 0}
    code_override = 0
    for item in reviews:
        severity = _text(_mapping(item).get("severity"), 10)
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        if _text(_mapping(item).get("overridden_by"), 20) == "code":
            code_override += 1
    total = len(reviews)
    approvable = total - severity_counts["block"]

    decisions = store.read_decisions(limit=200)
    trimmed = sum(1 for item in decisions if _mapping(_mapping(item).get("quota")).get("guarded"))

    experiments = store.read_experiments(limit=500, include_open=True)
    due = [item for item in experiments if is_due(item, today)]
    judged = [item for item in due if _text(item.get("verdict"), 20)]

    trace = _calls_from_trace(trace_dir)
    items = _int(orders.get("matched")) if isinstance(orders, dict) else 0
    items = items or sum(len(_list(item.get("variants"))) for item in proposals) or 1
    # Only the two per-product agents count. Director runs once per batch while
    # workshop/compliance scale with item count; mixing them in would make the
    # ratio jump whenever a director-only run happened to be the newest trace.
    per_item_calls = sum(
        _int(_mapping(trace["by_agent"].get(name)).get("calls")) for name in ("workshop", "compliance")
    )
    per_item = round(per_item_calls / items, 2) if per_item_calls else 0.0

    def pct(value: float) -> str:
        return f"{value * 100:.0f}%"

    completion = (approvable / planned) if planned else (approvable / produced if produced else 0.0)
    block_rate = (severity_counts["block"] / total) if total else 0.0
    trim_rate = (trimmed / len(decisions)) if decisions else 0.0
    verdict_rate = (len(judged) / len(due)) if due else 1.0

    def tone(key: str, value: float) -> str:
        target, direction = HEALTH_TARGETS[key]
        breached = value > target if direction == "higher" else value < target
        return "warn" if breached else "ok"

    metrics = [
        {
            "key": "completion",
            "label": "完成率",
            "display": pct(completion),
            "value": round(completion, 4),
            "note": f"{approvable}/{planned or produced} 版进入审批队列",
            "target": "≥80%",
        },
        {
            "key": "block_rate",
            "label": "否决率",
            "display": pct(block_rate),
            "value": round(block_rate, 4),
            "note": f"{severity_counts['block']}/{total} 版被否决",
            "target": "≤30%",
        },
        {
            "key": "code_override",
            "label": "代码覆盖否决",
            "display": str(code_override),
            "value": code_override,
            "note": "模型漏判、被硬禁兜住的次数" if code_override else "模型未漏判",
            "target": "0",
        },
        {
            "key": "quota_trim",
            "label": "名额裁剪率",
            "display": pct(trim_rate),
            "value": round(trim_rate, 4),
            "note": f"{trimmed}/{len(decisions)} 批被裁剪",
            "target": "≤20%",
        },
        {
            "key": "verdict_rate",
            "label": "实验判定率",
            "display": pct(verdict_rate),
            "value": round(verdict_rate, 4),
            "note": f"{len(judged)}/{len(due)} 条到期实验有结论",
            "target": "≥90%",
        },
        {
            "key": "calls_per_item",
            "label": "平均调用数",
            "display": f"{per_item:g}" if per_item else "—",
            "value": per_item,
            "note": f"最近一轮工坊+合规 {per_item_calls} 次 / {items} 个商品"
            if per_item_calls
            else "最近一轮没有工坊或合规调用",
            "target": "≤5",
        },
    ]
    for item in metrics:
        item["tone"] = tone(item["key"], float(item["value"]))

    return {
        "metrics": metrics,
        "severity": severity_counts,
        "trace": trace,
        "alerts": [f"{item['label']} {item['display']}（目标 {item['target']}）" for item in metrics if item["tone"] == "warn"],
    }
