"""Copywriter agent: turns a diagnosis into 2-3 genuinely different drafts.

It is allowed to choose the angle and the words. It is not allowed to invent
numbers, certifications or prices: it only picks an index into the price points
the code already computed.
"""

from __future__ import annotations

import json
from typing import Any

from ..llm import LLMClient, LLMError, TraceRecorder
from .base import MAX_BULLET_CHARS, MAX_BULLETS, MAX_TITLE_CHARS, WorkContext

SYSTEM_PROMPT = """You write B2B export product copy. You rewrite three fields only: title, bullets, description.

Hard rules, non-negotiable:
0. Write "title", every bullet and "description" in natural English for a US/EU B2B buyer, even when this conversation is in Chinese. Only "angle" and "rationale" stay Chinese. Non-English copy is rejected downstream.
1. Use only the facts in "known_facts". Never invent specifications, certifications, test reports, sales volume, rankings, customer names or awards.
2. No absolute claims: no best / No.1 / 100% / guaranteed / perfect / FDA approved unless that exact certification is in known_facts.
3. No brand names, competitor names, celebrities, movies or characters.
4. Each variant must take a different angle and a different opening. Rewording the same sentence three times is a failure.
5. Do not write any price number. Pick "price_index" from the given price_points instead.
6. Description must read like a sentence a human buyer would trust, not a keyword dump.
7. If "strategy" is present, variant i must implement strategy.angles[i]: use its angle and price_index, and address its focus. Do not substitute your own angle or price.
8. Every number you write must already appear in "known_facts" or the current title/bullets. Never add weight, size, material grade, setup time, battery life or test figures from general knowledge. If a spec is missing, describe the process instead of inventing the number.

Output JSON only, no markdown fence:
{"variants":[{"angle":"short label in Chinese","title":"...","bullets":["...","..."],"description":"...","price_index":0,"expected_lift":0.08,"rationale":"why this angle fits the evidence, in Chinese"}]}"""


def _facts(ctx: WorkContext) -> dict:
    product = ctx.product
    metrics = ctx.metrics
    return {
        "title": ctx.current_title or getattr(product, "title_en", ""),
        "category": getattr(product, "category_slug", "") or "",
        "moq_pcs": getattr(product, "moq", 0),
        "lead_time_days": getattr(product, "lead_time_days", 0),
        "incoterm": getattr(product, "incoterm", ""),
        "variants_in_stock": len(getattr(product, "variants", ()) or ()),
        "current_bullets": ctx.current_bullets,
        "current_description": ctx.current_description,
    }


def _metrics(ctx: WorkContext) -> dict:
    metrics = ctx.metrics
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


def _reviews(ctx: WorkContext) -> list[dict]:
    picked: list[dict] = []
    for item in ctx.reviews[:5]:
        picked.append(
            {
                "rating": item.get("rating"),
                "text": str(item.get("text") or "")[:280],
                "locale": item.get("locale") or "",
            }
        )
    return picked


def _price_points(ctx: WorkContext) -> list[dict]:
    return [{"index": index, "cents": cents} for index, cents in enumerate(ctx.price_points)]


class CopywriterAgent:
    role = "copywriter"

    def __init__(self, client: LLMClient, trace: TraceRecorder | None = None) -> None:
        self.client = client
        self.trace = trace

    def run(self, ctx: WorkContext) -> dict:
        payload: dict[str, Any] = {
            "known_facts": _facts(ctx),
            "performance_30d": _metrics(ctx),
            "recent_reviews": _reviews(ctx),
            "diagnosis": {
                "problem": ctx.task.get("problem"),
                "problem_label": ctx.task.get("problem_label"),
                "hypothesis": ctx.task.get("hypothesis"),
                "action": ctx.task.get("action"),
                "evidence": ctx.task.get("evidence"),
                "success_metric": ctx.task.get("success_metric"),
            },
            "price_points": _price_points(ctx),
            "variant_count": len(ctx.price_points),
            "market": ctx.market,
            "constraints": {
                "max_title_chars": MAX_TITLE_CHARS,
                "max_bullets": MAX_BULLETS,
                "max_bullet_chars": MAX_BULLET_CHARS,
                "banned_fields": ["status", "price number", "cost", "sku", "moq change"],
            },
        }
        strategy = ctx.task.get("strategy")
        if isinstance(strategy, dict) and strategy.get("planner") == "llm":
            payload["strategy"] = {
                "problem": strategy.get("problem"),
                "hypothesis": strategy.get("hypothesis"),
                "action": strategy.get("action"),
                "angles": strategy.get("angles") or [],
            }
        if ctx.critique.get("issues"):
            payload["previous_attempt"] = ctx.drafts
            payload["reviewer_issues"] = ctx.critique["issues"]

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        try:
            data, reply = self.client.chat_json(messages)
        except LLMError as exc:
            if self.trace is not None:
                self.trace.record(
                    agent=self.role,
                    ok=False,
                    model=self.client.settings.model,
                    messages=messages,
                    error=str(exc),
                    meta={"product_id": getattr(ctx.product, "product_id", "")},
                )
            raise

        if self.trace is not None:
            self.trace.record(
                agent=self.role,
                ok=True,
                model=reply.model,
                messages=messages,
                response=reply.text,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                latency_ms=reply.latency_ms,
                meta={"product_id": getattr(ctx.product, "product_id", "")},
            )
        return data
