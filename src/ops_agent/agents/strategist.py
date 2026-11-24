"""Strategist agent: choose the diagnosis, angles and price-point indices.

The rule engine supplies a small set of *supported* problem options and computes
all prices first. The model may choose among those options and explain why, but
it cannot create a problem with no supporting metrics or invent a price.
"""

from __future__ import annotations

import json
from typing import Any

from ..llm import LLMClient, LLMError, TraceRecorder
from .base import WorkContext

SYSTEM_PROMPT = """You are the strategist for a B2B export merchandising team.

Choose the most useful problem to solve from problem_options. These are the only
problems supported by the store's metrics. Read the actual reviews before deciding.
Then design 2-3 genuinely different copy strategies for the chosen problem.

Hard rules:
1. Never invent product facts, certifications, specifications, customers or results.
2. Use recent_reviews as evidence when they explain the problem. Refer to them by review_indices; do not quote imaginary reviews.
3. Each angle must attack the problem differently. Do not output three rewordings of one idea.
4. Never write a price amount. For each angle choose price_index from the chosen option's price_points.
5. Do not change MOQ, stock, cost, SKU, status, category or commercial terms.
6. hypothesis, action and success_metric must be specific to this product and its evidence, not generic slogans.

Output JSON only, no markdown fence:
{"problem":"one problem_options.problem","hypothesis":"Chinese, specific cause","action":"Chinese, concrete action","success_metric":"Chinese, observable metric","reason":"Chinese, why this problem and these angles","review_indices":[0],"angles":[{"angle":"short Chinese label","focus":"what this version must say, in Chinese","price_index":0}],"confidence":0.75}"""


def _metrics(product: Any) -> dict:
    metrics = getattr(product, "metrics", None)
    if metrics is None:
        return {}
    return {
        "pv_30d": int(getattr(metrics, "pv", 0)),
        "inquiries_30d": int(getattr(metrics, "inquiries", 0)),
        "orders_30d": int(getattr(metrics, "orders", 0)),
        "inquiry_rate_pct": round(getattr(metrics, "inquiry_rate", 0.0) * 100, 2),
        "order_rate_pct": round(getattr(metrics, "order_rate", 0.0) * 100, 2),
        "reviews": int(getattr(metrics, "reviews", 0)),
        "avg_rating": getattr(metrics, "avg_rating", None),
        "negative_reviews": int(getattr(metrics, "negative_reviews", 0)),
    }


class StrategistAgent:
    role = "strategist"

    def __init__(self, client: LLMClient, trace: TraceRecorder | None = None) -> None:
        self.client = client
        self.trace = trace

    def run(self, ctx: WorkContext) -> dict:
        options = ctx.task.get("strategy_options")
        if not isinstance(options, list) or not options:
            raise LLMError("no rule-supported strategy options")
        product = ctx.product
        payload = {
            "known_facts": {
                "product_id": getattr(product, "product_id", ""),
                "title": getattr(product, "title_en", ""),
                "category": getattr(product, "category_slug", "") or "",
                "moq_pcs": getattr(product, "moq", 0),
                "lead_time_days": getattr(product, "lead_time_days", 0),
                "incoterm": getattr(product, "incoterm", ""),
                "stock_by_variant": [
                    {"price_cents": item.price_cents, "stock": item.stock}
                    for item in (getattr(product, "variants", ()) or ())
                ],
            },
            "performance_30d": _metrics(product),
            "recent_reviews": [
                {
                    "index": index,
                    "rating": item.get("rating"),
                    "text": str(item.get("text") or "")[:300],
                    "country": item.get("country") or item.get("locale") or "",
                }
                for index, item in enumerate(ctx.reviews[:5])
            ],
            "rule_diagnosis": {
                "problem": ctx.task.get("problem"),
                "score": ctx.task.get("score"),
                "evidence": ctx.task.get("evidence"),
            },
            "problem_options": options,
            "instruction": "Select one supported problem and design one angle per price point. Use review evidence only when relevant.",
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            data, reply = self.client.chat_json(messages, temperature=0.25)
        except LLMError as exc:
            self._record(messages, ctx, error=str(exc))
            raise
        self._record(messages, ctx, reply=reply)
        return self._normalize(data, ctx)

    def _record(self, messages: list[dict], ctx: WorkContext, *, reply=None, error: str = "") -> None:
        if self.trace is None:
            return
        self.trace.record(
            agent=self.role,
            ok=error == "",
            model=reply.model if reply is not None else self.client.settings.model,
            messages=messages,
            response=reply.text if reply is not None else "",
            error=error,
            prompt_tokens=reply.prompt_tokens if reply is not None else 0,
            completion_tokens=reply.completion_tokens if reply is not None else 0,
            latency_ms=reply.latency_ms if reply is not None else 0,
            meta={"product_id": getattr(ctx.product, "product_id", "")},
        )

    @staticmethod
    def _normalize(data: dict, ctx: WorkContext) -> dict:
        options = ctx.task["strategy_options"]
        problem = str(data.get("problem") or "").strip()
        option = next((item for item in options if item.get("problem") == problem), None)
        if option is None:
            raise LLMError(f"unsupported problem from model: {problem or '<empty>'}")

        points = [int(item["cents"]) for item in option.get("price_points") or []]
        if not points:
            raise LLMError("chosen strategy has no price points")
        raw_angles = data.get("angles")
        if not isinstance(raw_angles, list):
            raise LLMError("strategy angles is not a list")

        violations: list[str] = []
        angles: list[dict] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_angles[: len(points)]):
            if not isinstance(raw, dict):
                violations.append(f"angle[{index}] dropped: not an object")
                continue
            label = str(raw.get("angle") or "").strip()[:60]
            if not label:
                violations.append(f"angle[{index}] dropped: missing label")
                continue
            if label.casefold() in seen:
                violations.append(f"angle[{index}] dropped: duplicate angle")
                continue
            seen.add(label.casefold())
            price_index = raw.get("price_index", 0)
            if isinstance(price_index, bool) or not isinstance(price_index, int):
                violations.append(f"angle[{index}]: invalid price_index forced to 0")
                price_index = 0
            if not 0 <= price_index < len(points):
                violations.append(f"angle[{index}]: price_index out of range forced to 0")
                price_index = 0
            angles.append(
                {
                    "angle": label,
                    "focus": str(raw.get("focus") or "").strip()[:240],
                    "price_index": price_index,
                }
            )
        if not angles:
            raise LLMError("model produced no usable strategy angle")

        review_evidence: list[str] = []
        raw_indices = data.get("review_indices")
        if isinstance(raw_indices, list):
            for raw_index in raw_indices[:3]:
                if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                    continue
                if not 0 <= raw_index < len(ctx.reviews[:5]):
                    violations.append(f"review index out of range dropped: {raw_index}")
                    continue
                text = str(ctx.reviews[raw_index].get("text") or "").strip()
                if text:
                    review_evidence.append(f"评价原文：{text[:120]}")

        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        evidence = list(option.get("evidence") or ctx.task.get("evidence") or [])
        evidence.extend(review_evidence)
        return {
            "planner": "llm",
            "problem": problem,
            "problem_label": option.get("problem_label") or problem,
            "hypothesis": str(data.get("hypothesis") or option.get("hypothesis") or "").strip()[:300],
            "action": str(data.get("action") or option.get("action") or "").strip()[:300],
            "success_metric": str(data.get("success_metric") or option.get("success_metric") or "").strip()[:160],
            "reason": str(data.get("reason") or "").strip()[:600],
            "confidence": round(min(1.0, max(0.0, confidence)), 3),
            "evidence": evidence[:10],
            "angles": angles,
            "violations": violations,
        }
