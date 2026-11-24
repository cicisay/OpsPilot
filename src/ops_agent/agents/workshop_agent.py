"""内容工坊 Agent：接任务单，自己决定出几版，自己挑毛病再改。

和旧的 ``workshop.py`` + ``CopyOrchestrator`` 的区别：

* **版数由模型定**，不是固定 3 版。任务单给出 ``variant_min..variant_max``，
  模型在其中选，并说明理由（价格带宽、库存深度、问题复杂度）。
* **自评是工坊自己的步骤**，不是独立 Critic。每版必须交三项自检：事实、差异化、
  可读性；自评不过就定向重写，最多 2 轮。
* **失败不再回落规则模板**。旧实现在模型写错时悄悄给一套模板方案，运营台上看起来
  "成功了"，实际是另一条代码路径。现在把校验错误原样交回模型重写；重写仍失败就
  该商品失败并记录原因，绝不冒充模型产出。

代码仍然只管三件事：红线（英文、数字可溯源、价位点）、契约（字段与版数区间）、
调用预算。文案写什么、几版、什么角度，全归模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts import (
    MAX_VARIANTS,
    MIN_VARIANTS,
    normalize_proposal,
    validate_proposal,
)
from ..llm import LLMClient, LLMError, TraceRecorder, parse_json_object
from ..store_metrics import entry_variant
from .base import RedLineGuard, WorkContext, known_facts_text

# Five variants with English copy plus a Chinese self-review do not fit in the
# default 4096; the JSON gets cut mid-string and the whole task is wasted.
WORKSHOP_MAX_TOKENS = 6144

SELF_CHECK_KEYS = ("facts", "differentiation", "readability")

SYSTEM_PROMPT = """You are the content workshop of a B2B export store.
You receive one task order from the operations director and produce the listing
variants that task asks for. You also review your own drafts before handing them over.

Decide in this order:
1. How many variants to produce. The task gives variant_min and variant_max.
   Choose inside that range and justify it from the task itself: width of the
   price band, stock cover, how many distinct buyer objections the evidence
   shows. Do not default to the maximum.
2. One distinct angle per variant. The angle must be visible in the title, not
   only in the rationale. Two variants that differ by synonyms are a failure.
3. Write the copy, then check it yourself.

Hard rules, non-negotiable:
- title, every bullet and description are English for a US/EU B2B buyer.
  angle, variant_reason, material_brief and self_review stay Chinese.
- Use ONLY the facts in known_facts. Never invent specifications, dimensions,
  weight, material grade, certification, test result, sales volume, ranking,
  customer name or award. If the listing does not state a number, describe the
  process or the benefit without a number. This is enforced by code: any number
  that is not in known_facts gets the variant dropped.
- No absolute or guaranteed claims: no 100%, guaranteed, best, No.1, cheapest,
  perfect, risk-free, cure. No brand, competitor, celebrity or film references.
- Do not write a price number. Pick price_index from price_points.
- bullets: 2 to 6 items, each a concrete buying reason, not a keyword dump.
- material_brief: 1 to 3 items for the designer. State subject, caption and
  forbidden elements (no brand logo, no celebrity, no before/after medical claim).
- self_check: exactly three short Chinese sentences, one each for facts,
  differentiation and readability. Say what you verified, not "OK".
- self_review: one Chinese sentence summarizing what you fixed after the check.

Output JSON only, no markdown fence:
{
  "variant_count": 3,
  "variant_reason": "Chinese, why this many, citing the task",
  "variants": [
    {
      "angle": "Chinese label",
      "title": "English title",
      "bullets": ["English bullet", "English bullet"],
      "description": "English paragraph",
      "price_index": 0,
      "expected_lift": 0.08,
      "material_brief": ["Chinese, subject", "Chinese, caption", "Chinese, forbidden"],
      "self_check": {"facts": "...", "differentiation": "...", "readability": "..."},
      "self_review": "..."
    }
  ]
}"""


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def variant_range(task: dict) -> tuple[int, int]:
    """The task's own bounds, clamped to what the contract allows."""
    low = _int(task.get("variant_min"), MIN_VARIANTS)
    high = _int(task.get("variant_max"), low)
    low = max(MIN_VARIANTS, min(MAX_VARIANTS, low))
    high = max(low, min(MAX_VARIANTS, high))
    return low, high


def price_points_for(task: dict, entry, count: int) -> list[int]:
    """Concrete prices for the variants. The model only ever picks an index."""
    count = max(MIN_VARIANTS, min(MAX_VARIANTS, int(count)))
    explicit = task.get("price_points")
    if isinstance(explicit, list):
        cleaned = [int(item) for item in explicit if _int(item) > 0]
        if len(cleaned) >= count and len(set(cleaned[:count])) == count:
            return cleaned[:count]
    low, high = task.get("price_low_cents"), task.get("price_high_cents")
    if not low or not high:
        band = task.get("price_band_cents") or []
        if isinstance(band, (list, tuple)) and len(band) >= 2:
            low, high = band[0], band[1]
    low = _int(low, getattr(entry, "price_cents", 0))
    high = _int(high, low)
    if low > high:
        low, high = high, low
    base = max(1, low)
    if high <= base:
        return [base] * count
    step = (high - base) / (count - 1)
    return [int(round(base + step * index)) for index in range(count)]


def _facts(ctx: WorkContext) -> dict:
    product = ctx.product
    return {
        "title": ctx.current_title or getattr(product, "title_en", ""),
        "category": getattr(product, "category_slug", "") or "",
        "moq_pcs": getattr(product, "moq", 0),
        "lead_time_days": getattr(product, "lead_time_days", 0),
        "incoterm": getattr(product, "incoterm", ""),
        "current_bullets": ctx.current_bullets,
        "current_description": ctx.current_description,
    }


def _task_slice(task: dict) -> dict:
    return {
        "task_id": _text(task.get("task_id")),
        "problem": _text(task.get("problem")),
        "problem_label": _text(task.get("problem_label")),
        "problem_detail": _text(task.get("problem_detail")),
        "evidence": _str_list(task.get("evidence")),
        "hypothesis": _text(task.get("hypothesis")),
        "action": _text(task.get("action")),
        "success_metric": _text(task.get("success_metric")),
        "min_lift": task.get("min_lift"),
        "must_include": _str_list(task.get("must_include")),
        "forbidden": _str_list(task.get("forbidden")),
    }


def build_payload(
    ctx: WorkContext,
    *,
    variant_min: int,
    variant_max: int,
    previous: dict | None = None,
    issues: list[str] | None = None,
) -> dict:
    """Only what the workshop needs: the order, the facts, the price ladder."""
    payload: dict[str, Any] = {
        "task": _task_slice(ctx.task),
        "known_facts": _facts(ctx),
        "market": ctx.market,
        "reviews": [
            {"rating": item.get("rating"), "text": _text(item.get("text"))[:240]}
            for item in ctx.reviews[:4]
        ],
        "price_points": [
            {"index": index, "cents": cents, "amount": f"{cents / 100:.2f}"}
            for index, cents in enumerate(ctx.price_points)
        ],
        "variant_min": variant_min,
        "variant_max": variant_max,
        "instruction": (
            "Produce variant_count between variant_min and variant_max. "
            "Every variant needs a different angle, English copy, a price_index, "
            "a material_brief of 1-3 items and a three-item self_check."
        ),
    }
    if issues:
        if previous is not None:
            payload["previous_attempt"] = {
                "variant_count": previous.get("variant_count"),
                "variants": previous.get("variants")
                or previous.get("_usable_variants")
                or [],
            }
        payload["validation_issues"] = issues[:12]
        payload["instruction"] = (
            "Your previous reply failed validation. Fix only the listed problems, "
            "keep every variant that was already valid, and reply with the complete "
            "JSON object again. Keep descriptions under 60 words."
        )
    return payload


@dataclass
class WorkshopResult:
    task_id: str
    product_id: str
    proposal: dict
    attempts: int = 1
    issues: list[str] = field(default_factory=list)
    calls: int = 0
    model: str = ""
    planner: str = "llm"
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def variants(self) -> list[dict]:
        return self.proposal.get("variants") or []

    def as_payload(self) -> dict:
        return {
            "task_id": self.task_id,
            "product_id": self.product_id,
            "proposal": self.proposal,
            "attempts": self.attempts,
            "issues": self.issues,
            "model": self.model,
            "planner": self.planner,
            "generated_at": self.generated_at,
        }


class WorkshopError(Exception):
    """The workshop could not produce a proposal that passes the contract."""

    def __init__(self, product_id: str, issues: list[str]) -> None:
        self.product_id = product_id
        self.issues = list(issues)
        super().__init__(f"workshop failed for {product_id}: " + "; ".join(self.issues[:4]))


class WorkshopAgent:
    """One draft call plus bounded revisions, all through the same red lines."""

    role = "workshop"

    def __init__(self, client: LLMClient, trace: TraceRecorder | None = None) -> None:
        self.client = client
        self.trace = trace

    def draft(self, payload: dict) -> tuple[dict, str]:
        return self._call(payload, temperature=0.5)

    def revise(self, payload: dict) -> tuple[dict, str]:
        # Lower temperature: a revision is a correction, not a new brainstorm.
        return self._call(payload, temperature=0.25)

    def _call(self, payload: dict, *, temperature: float) -> tuple[dict, str]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            reply = self.client.chat(
                messages, temperature=temperature, max_tokens=WORKSHOP_MAX_TOKENS
            )
        except LLMError as exc:
            self._record(messages, error=str(exc))
            raise
        try:
            data = parse_json_object(reply.text)
        except LLMError as exc:
            self._record(messages, reply=reply, error=str(exc))
            raise
        self._record(messages, reply=reply)
        return data, reply.text

    def _record(self, messages: list[dict], *, reply=None, error: str = "") -> None:
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
            meta={"agent": self.role},
        )


def _check_self_review(variants: list[dict]) -> list[str]:
    """FR-516: three named checks per variant, not a rubber-stamp sentence."""
    issues: list[str] = []
    for index, variant in enumerate(variants):
        check = variant.get("self_check") or {}
        missing = [key for key in SELF_CHECK_KEYS if not _text(check.get(key))]
        if missing:
            issues.append(f"variant[{index}]: self_check missing {missing}")
        brief = variant.get("material_brief") or []
        if not brief:
            issues.append(f"variant[{index}]: material_brief is empty")
        elif len(brief) > 3:
            issues.append(f"variant[{index}]: material_brief has {len(brief)} items, max 3")
    return issues


def normalize_attempt(
    raw: dict,
    *,
    task: dict,
    ctx: WorkContext,
    guard: RedLineGuard,
    variant_min: int,
    variant_max: int,
) -> tuple[dict, list[str]]:
    """Model reply -> contract-shaped proposal, plus everything that is wrong."""
    issues: list[str] = []
    product_id = (
        _text(raw.get("product_id"))
        or _text(task.get("product_id"))
        or getattr(ctx.product, "product_id", "")
    )
    # A task id is the join key back to the director's order. Legacy task files
    # predate it, so fall back to run_id:product_id rather than failing the task.
    task_id = (
        _text(raw.get("task_id"))
        or _text(task.get("task_id"))
        or f"{_text(task.get('run_id'))}:{product_id}".strip(":")
    )
    doc = normalize_proposal(
        {
            "task_id": task_id,
            "product_id": product_id,
            "variant_count": raw.get("variant_count"),
            "variant_reason": _text(raw.get("variant_reason")),
            "variants": raw.get("variants"),
        }
    )

    guarded = guard.check_variants(doc.get("variants") or [])
    issues.extend(guarded.violations)
    variants = guarded.variants
    # The guard caps at variant_max, so count what the model actually sent
    # before the cap, otherwise an over-eager model looks like a compliant one.
    requested = len(doc.get("variants") or [])
    if requested > variant_max:
        issues.append(f"{requested} variants produced, trimmed to variant_max {variant_max}")

    # The guard returns the cleaned shape; keep contract identity on top of it.
    for index, variant in enumerate(variants):
        variant["index"] = index
        variant.setdefault("variant_key", f"{doc['product_id']}-v{index + 1}")
        variant["variant_id"] = variant["variant_key"]

    if len(variants) > variant_max:
        variants = variants[:variant_max]
    if len(variants) < variant_min:
        issues.append(
            f"only {len(variants)} usable variants, task requires at least {variant_min}"
        )
    if not _text(doc.get("variant_reason")):
        issues.append("variant_reason is empty: say why this many variants")

    doc["variant_count"] = len(variants)
    doc["variants"] = variants
    issues.extend(_check_self_review(variants))

    contract_errors = validate_proposal(doc, task)
    issues.extend(contract_errors)
    return doc, issues


def _score(doc: dict, issues: list[str], variant_min: int) -> tuple[int, int, int]:
    """Prefer an attempt that clears the minimum, then more variants, then fewer issues."""
    count = len(doc.get("variants") or [])
    return (1 if count >= variant_min else 0, count, -len(issues))


def run_workshop_task(
    *,
    task: dict,
    product,
    client: LLMClient,
    market: str = "US",
    detail: dict | None = None,
    reviews: list[dict] | None = None,
    trace: TraceRecorder | None = None,
    max_rounds: int | None = None,
) -> WorkshopResult:
    """Draft, self-check, revise. Raises WorkshopError if nothing usable survives."""
    if not client.enabled:
        raise LLMError(f"model not configured: {client.settings.describe()}")

    detail = detail or {}
    english = (detail.get("i18n") or {}).get("En") or {}
    current_title = _text(english.get("title")) or getattr(product, "title_en", "")
    current_bullets = _str_list(english.get("bullets"))
    current_description = _text(english.get("desc"))

    entry = entry_variant(product)
    variant_min, variant_max = variant_range(task)
    prices = price_points_for(task, entry, variant_max)

    ctx = WorkContext(
        product=product,
        task=task,
        market=market,
        metrics=getattr(product, "metrics", None),
        reviews=list(reviews or []),
        price_points=prices,
        current_title=current_title,
        current_bullets=current_bullets,
        current_description=current_description,
    )
    guard = RedLineGuard(prices, limit=variant_max, facts_text=known_facts_text(ctx))
    agent = WorkshopAgent(client, trace)

    rounds_allowed = client.settings.max_revision_rounds if max_rounds is None else max_rounds
    # A product with 5 variants needs 3 calls at most; never exceed the budget
    # the operator set for one item.
    max_calls = max(1, min(int(client.settings.max_calls_per_item), 1 + max(0, int(rounds_allowed))))

    payload = build_payload(ctx, variant_min=variant_min, variant_max=variant_max)
    best: dict | None = None
    best_issues: list[str] = []
    previous_raw: dict | None = None
    last_error: LLMError | None = None
    calls = 0

    for attempt in range(1, max_calls + 1):
        try:
            data, _ = agent.draft(payload) if attempt == 1 else agent.revise(payload)
        except LLMError as exc:
            if attempt >= max_calls:
                if last_error is not None:
                    raise LLMError(f"{last_error} (retry failed: {exc})") from None
                raise
            if last_error is None:
                # Keep the first failure: it is the one that explains the cause.
                last_error = exc
            # A truncated or malformed reply is recoverable: ask again with the
            # parser's own message and an explicit brevity instruction, instead
            # of losing the product to one bad generation.
            payload = build_payload(
                ctx,
                variant_min=variant_min,
                variant_max=variant_max,
                issues=[f"reply could not be parsed: {exc}"],
            )
            calls += 1
            continue
        calls += 1
        doc, issues = normalize_attempt(
            data,
            task=task,
            ctx=ctx,
            guard=guard,
            variant_min=variant_min,
            variant_max=variant_max,
        )
        if best is None or _score(doc, issues, variant_min) > _score(
            best, best_issues, variant_min
        ):
            best, best_issues = doc, issues
        if not issues:
            break
        if attempt >= max_calls:
            break
        # Capping an over-eager model is not a model error; asking it to rewrite
        # a valid proposal only burns a call and risks a worse one.
        blocking = [item for item in issues if "trimmed to variant_max" not in item]
        if not blocking:
            break
        previous_raw = {"variant_count": doc.get("variant_count"), "variants": doc.get("variants")}
        payload = build_payload(
            ctx,
            variant_min=variant_min,
            variant_max=variant_max,
            previous=previous_raw,
            issues=issues,
        )

    if best is None:
        raise WorkshopError(
            _text(task.get("product_id")),
            ["the model produced no parsable reply within the call budget"],
        )
    if not best.get("variants"):
        raise WorkshopError(
            _text(task.get("product_id")),
            best_issues or ["the model produced no variant that passed the red lines"],
        )
    if len(best["variants"]) < variant_min:
        raise WorkshopError(
            _text(task.get("product_id")),
            best_issues
            + [f"only {len(best['variants'])} usable variants, requires {variant_min}"],
        )

    best["variant_count"] = len(best["variants"])
    return WorkshopResult(
        task_id=_text(task.get("task_id")) or f"{_text(task.get('run_id'))}:{_text(task.get('product_id'))}",
        product_id=_text(task.get("product_id")),
        proposal=best,
        attempts=calls,
        issues=best_issues,
        calls=calls,
        model=client.settings.model,
        planner="llm",
    )
