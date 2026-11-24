"""P2 合规验收探针：用真实模型跑 5 种绝对化改写 + 1 个品牌硬禁。

用法：
    python scripts/p2_compliance_probe.py

验收口径（PRD 14.1）：
- 5 种绝对化改写至少识别 4 种（block 或 warn 均可，block 更严格）；
- 品牌词即使模型判 pass，代码也必须强制 block，并记 overridden_by=code；
- 干净文案不得被误杀（pass）。

脚本只读站点事实、不写任何站点数据，结果打印到终端。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents.compliance_agent import run_compliance  # noqa: E402
from ops_agent.config import llm_settings  # noqa: E402
from ops_agent.llm import LLMClient, LLMError, TraceRecorder  # noqa: E402
from ops_agent.site_platform import SiteProduct, SiteVariant  # noqa: E402

VARIANTS = [
    ("abs-1", "100% effective stain removal for every kitchen surface."),
    ("abs-2", "Guaranteed to work every single time, or we refund you."),
    ("abs-3", "Zero failure rate across 10,000 restaurant kitchens."),
    ("abs-4", "Absolutely effective on the toughest grease, no exceptions."),
    ("abs-5", "You will be satisfied with every order, we promise."),
    ("brand", "Nike-grade grip and finish for professional kitchens."),
    ("clean", "Stoneware mug with a reactive glaze; batch QC before shipment."),
]


def build_proposal() -> dict:
    variants = []
    for index, (key, sentence) in enumerate(VARIANTS):
        variants.append(
            {
                "variant_key": key,
                "angle": f"探针 {index + 1}",
                "title": "Ceramic Mug Set - Kitchen Line",
                "bullets": [sentence],
                "description": sentence,
                "material_brief": ["主体：厨房场景", "禁用：品牌 logo"],
                "price_cents": 620,
            }
        )
    return {"product_id": "probe", "variants": variants}


def build_product() -> SiteProduct:
    variant = SiteVariant("v1", "PROBE-01", 620, 400, 100, {})
    return SiteProduct(
        product_id="probe",
        slug="probe",
        title_en="Ceramic Mug Set",
        status="active",
        category_id="c1",
        category_slug="home-kitchen",
        moq=100,
        lead_time_days=18,
        incoterm="FOB",
        variants=(variant,),
        metrics=None,
    )


def main() -> int:
    settings = llm_settings()
    if not settings.enabled:
        print(f"模型未配置：{settings.describe()}")
        return 2
    client = LLMClient(settings=settings)
    trace = TraceRecorder()
    try:
        result = run_compliance(
            proposal=build_proposal(),
            product=build_product(),
            client=client,
            market="US",
            trace=trace,
        )
    except LLMError as exc:
        print(f"调用失败：{exc}")
        return 1

    caught = 0
    for verdict in result.verdicts:
        key = verdict["variant_key"]
        severity = verdict["severity"]
        if key.startswith("abs") and severity in ("block", "warn"):
            caught += 1
        mark = {"block": "否决", "warn": "提示", "pass": "通过"}.get(severity, severity)
        override = " · 代码强制" if verdict.get("overridden_by") == "code" else ""
        rules = "；".join(
            str(item.get("rule") or "") for item in verdict.get("findings") or []
        )[:70]
        print(f"{key:6} {mark}{override:8} {rules}")

    brand = next((item for item in result.verdicts if item["variant_key"] == "brand"), {})
    clean = next((item for item in result.verdicts if item["variant_key"] == "clean"), {})
    print()
    print(f"绝对化识别 {caught}/5 · 品牌硬禁 {brand.get('severity')} · 干净文案 {clean.get('severity')}")
    print(f"调用 {result.calls} 次 · 模型 {result.model} · trace {trace.summary()['path']}")
    ok = caught >= 4 and brand.get("severity") == "block" and clean.get("severity") == "pass"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
