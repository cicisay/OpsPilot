"""Critic agent: reads the drafts the way a picky merchandiser would.

It exists to break the "three variants that are the same sentence" failure mode
and to catch claims that contradict the real product facts.
"""

from __future__ import annotations

import json

from ..llm import LLMClient, LLMError, TraceRecorder
from .base import WorkContext

SYSTEM_PROMPT = """You are a strict merchandising reviewer for a B2B export store.

You receive product facts, 30-day performance, recent negative reviews and 2-3 candidate copies.
Score them and say whether they are good enough to show a human approver.

Check four things:
1. Targeting: does the copy answer the specific reason customers complained or did not convert?
2. Fact consistency: does any sentence contradict known_facts (numbers, lead time, MOQ, certifications)?
3. Differentiation: are the variants actually different in angle, not just synonyms?
4. Risk: absolute claims, unverifiable certifications, brand or celebrity references.
5. Strategy alignment: when strategy is present, each draft must address its planned angle and focus.

Output JSON only:
{"score":0.0,"verdict":"pass","issues":["..."],"keep_indices":[0,1]}
- score: 0 to 1, one decimal is enough
- verdict: "pass" if score >= 0.6 and no fact/risk issue, otherwise "revise"
- issues: specific, actionable, each under 60 Chinese characters; empty list when passing
- keep_indices: which variants are worth keeping as-is"""


class CriticAgent:
    role = "critic"

    def __init__(self, client: LLMClient, trace: TraceRecorder | None = None) -> None:
        self.client = client
        self.trace = trace

    def run(self, ctx: WorkContext) -> dict:
        payload = {
            "known_facts": {
                "title": ctx.current_title,
                "category": getattr(ctx.product, "category_slug", "") or "",
                "moq_pcs": getattr(ctx.product, "moq", 0),
                "lead_time_days": getattr(ctx.product, "lead_time_days", 0),
                "incoterm": getattr(ctx.product, "incoterm", ""),
                "current_bullets": ctx.current_bullets,
                "current_description": ctx.current_description,
            },
            "problem": ctx.task.get("problem"),
            "strategy": ctx.task.get("strategy") or {},
            "evidence": ctx.task.get("evidence"),
            "recent_reviews": [
                {"rating": item.get("rating"), "text": str(item.get("text") or "")[:200]}
                for item in ctx.reviews[:5]
            ],
            "drafts": [
                {
                    "index": index,
                    "angle": draft.get("angle"),
                    "title": draft.get("title"),
                    "bullets": draft.get("bullets"),
                    "description": draft.get("description"),
                }
                for index, draft in enumerate(ctx.drafts)
            ],
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        try:
            data, reply = self.client.chat_json(messages, temperature=0.2)
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
        return self._normalize(data)

    @staticmethod
    def _normalize(data: dict) -> dict:
        try:
            score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in {"pass", "revise"}:
            verdict = "pass" if score >= 0.6 else "revise"
        issues = data.get("issues")
        if not isinstance(issues, list):
            issues = []
        keep = data.get("keep_indices")
        if not isinstance(keep, list):
            keep = []
        return {
            "score": round(min(1.0, max(0.0, score)), 3),
            "verdict": verdict,
            "issues": [str(item)[:120] for item in issues][:6],
            "keep_indices": [int(item) for item in keep if isinstance(item, int)],
        }
