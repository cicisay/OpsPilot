"""P3 探针：经验归纳能不能只写证据支持得住的东西。

真实跑批里三条实验全部判为"不确定"（订单率样本不足），模型只归纳出一条
元经验。这个脚本补另一种情况：给模型几条**有明确结论**的实验，看它会不会
写出可复用、且数字站得住的经验。

实验数据是合成的，只在本进程里用，不写台账。

    python scripts/p3_lesson_probe.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.config import llm_settings  # noqa: E402
from ops_agent.lessons import LessonError, derive_lessons  # noqa: E402
from ops_agent.llm import LLMClient  # noqa: E402

RECORDS = [
    {
        "experiment_id": "probe-1",
        "product_id": "probe-vacuum-bottle",
        "category": "home-kitchen",
        "angle": "把盖型差异写进标题与订单文件",
        "hypothesis": "把盖型写进标题可减少错发",
        "problem": "lid_mismatch",
        "success_metric": "order_rate_pct从当前0.24%提升至至少0.28%",
        "verdict": "supported",
        "verdict_cn": "成立",
        "outcome": {
            "lift": 0.34,
            "min_lift": 0.15,
            "p_value": 0.021,
            "baseline": {"order_rate": 0.0024, "click_rate": 0.051, "return_rate": 0.0},
            "treated": {"order_rate": 0.0032, "click_rate": 0.058, "return_rate": 0.0},
            "note": "提升 34.0% ≥ 目标 15.0%，p=0.021",
        },
    },
    {
        "experiment_id": "probe-2",
        "product_id": "probe-kraft-mailer",
        "category": "packaging-printing",
        "angle": "标题前置交付周期与起订量",
        "hypothesis": "采购方最关心交期与 MOQ，前置可提升询盘",
        "problem": "low_inquiry",
        "success_metric": "click_rate从当前5.1%提升至至少5.9%",
        "verdict": "supported",
        "verdict_cn": "成立",
        "outcome": {
            "lift": 0.22,
            "min_lift": 0.15,
            "p_value": 0.004,
            "baseline": {"order_rate": 0.0011, "click_rate": 0.051, "return_rate": 0.0},
            "treated": {"order_rate": 0.0012, "click_rate": 0.062, "return_rate": 0.0},
            "note": "提升 22.0% ≥ 目标 15.0%，p=0.004",
        },
    },
    {
        "experiment_id": "probe-3",
        "product_id": "probe-cast-iron",
        "category": "home-kitchen",
        "angle": "单纯降价 8% 不改文案",
        "hypothesis": "降价能直接带来订单",
        "problem": "low_order",
        "success_metric": "order_rate_pct从当前0.24%提升至至少0.28%",
        "verdict": "rejected",
        "verdict_cn": "不成立",
        "outcome": {
            "lift": -0.12,
            "min_lift": 0.15,
            "p_value": 0.031,
            "baseline": {"order_rate": 0.0024, "click_rate": 0.05, "return_rate": 0.0},
            "treated": {"order_rate": 0.0021, "click_rate": 0.049, "return_rate": 0.0},
            "note": "提升 -12.0% ≤ 0 且显著，p=0.031",
        },
    },
]


def main() -> int:
    settings = llm_settings()
    if not settings.enabled:
        print(f"模型未配置（{settings.describe()}），无法运行探针。")
        return 1

    client = LLMClient(settings=settings)
    print(f"模型: {settings.describe()} · 输入 {len(RECORDS)} 条已判定实验")
    try:
        lessons, model, issues = derive_lessons(client, RECORDS, [])
    except LessonError as exc:
        print(f"[FAIL] {exc}")
        return 1

    print(f"\n产出 {len(lessons)} 条经验（{model}）")
    for item in lessons:
        print(
            f"  [{item['direction']}] {item['scope']} · {item['statement']}"
            f"（证据 {item['evidence_count']} · 置信 {item['confidence']}）"
        )
    if issues:
        print("\n校验丢掉的：")
        for issue in issues:
            print(f"  - {issue}")

    numbers = [item["statement"] for item in lessons if any(ch.isdigit() for ch in item["statement"])]
    print(f"\n带数字的经验 {len(numbers)} 条（数字须来自证据，否则会被丢弃）")
    print(json.dumps({"lessons": lessons}, ensure_ascii=False, indent=2)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
