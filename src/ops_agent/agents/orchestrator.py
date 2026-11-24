"""Orchestrator: draft -> critique -> revise, bounded by calls and rounds.

This is the only place that decides "call the model again". Everything it
returns has already passed ``RedLineGuard``, so downstream code never sees a
price the model made up or a field it was not allowed to touch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import LLMClient, LLMError, LLMNotConfigured, TraceRecorder
from .base import RedLineGuard, WorkContext, known_facts_text
from .copywriter import CopywriterAgent
from .critic import CriticAgent


@dataclass
class CopyOutcome:
    variants: list[dict] = field(default_factory=list)
    critique: dict = field(default_factory=dict)
    rounds: int = 0
    violations: list[str] = field(default_factory=list)
    planner: str = "llm"
    calls: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.variants)


def _enforce_strategy(
    variants: list[dict], strategy: dict, price_points: list[int], violations: list[str]
) -> None:
    """The strategist owns angle/price choice; the copywriter owns words only."""
    if not isinstance(strategy, dict) or strategy.get("planner") != "llm":
        return
    angles = strategy.get("angles")
    if not isinstance(angles, list):
        return
    for index, variant in enumerate(variants[: len(angles)]):
        planned = angles[index]
        if not isinstance(planned, dict):
            continue
        label = str(planned.get("angle") or "").strip()
        if label and variant.get("angle") != label:
            violations.append(f"variant[{index}]: angle aligned to strategist")
            variant["angle"] = label
        price_index = planned.get("price_index")
        if isinstance(price_index, int) and not isinstance(price_index, bool) and 0 <= price_index < len(price_points):
            if variant.get("price_index") != price_index:
                violations.append(f"variant[{index}]: price_index aligned to strategist")
            variant["price_index"] = price_index
            variant["price_cents"] = price_points[price_index]
        variant["strategy_focus"] = str(planned.get("focus") or "")[:240]


class CopyOrchestrator:
    def __init__(self, client: LLMClient | None = None, trace: TraceRecorder | None = None) -> None:
        self.client = client or LLMClient()
        self.trace = trace
        self._writer = CopywriterAgent(self.client, trace)
        self._critic = CriticAgent(self.client, trace)

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    @property
    def model_label(self) -> str:
        return self.client.settings.model

    def generate(self, ctx: WorkContext) -> CopyOutcome:
        if not self.enabled:
            raise LLMNotConfigured(f"model not configured: {self.client.settings.describe()}")
        if not ctx.price_points:
            raise LLMError("no price points available")

        limit = len(ctx.price_points)
        guard = RedLineGuard(ctx.price_points, limit=limit, facts_text=known_facts_text(ctx))
        strategy_calls = 1 if (ctx.task.get("strategy") or {}).get("planner") == "llm" else 0
        max_calls = max(1, self.client.settings.max_calls_per_item - strategy_calls)
        max_rounds = max(1, self.client.settings.max_revision_rounds)

        outcome = CopyOutcome()
        calls = 0

        data = self._writer.run(ctx)
        calls += 1
        guarded = guard.check_variants(data.get("variants"))
        outcome.violations.extend(guarded.violations)
        outcome.variants = guarded.variants
        _enforce_strategy(outcome.variants, ctx.task.get("strategy"), ctx.price_points, outcome.violations)
        ctx.drafts = outcome.variants

        while outcome.variants and calls + 1 <= max_calls and outcome.rounds < max_rounds:
            critique = self._critic.run(ctx)
            calls += 1
            outcome.rounds += 1
            outcome.critique = critique
            ctx.critique = critique
            if critique.get("verdict") == "pass":
                break

            revised = self._writer.run(ctx)
            calls += 1
            guarded = guard.check_variants(revised.get("variants"))
            outcome.violations.extend(guarded.violations)
            if not guarded.variants:
                break
            outcome.variants = guarded.variants
            _enforce_strategy(outcome.variants, ctx.task.get("strategy"), ctx.price_points, outcome.violations)
            ctx.drafts = outcome.variants

        outcome.calls = calls
        if not outcome.variants:
            raise LLMError("model produced no usable variant after guard")
        return outcome
