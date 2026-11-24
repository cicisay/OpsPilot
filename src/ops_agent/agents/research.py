"""The research agent: a director that decides *and* looks things up itself.

The difference from ``director.py`` is not the output format, it is who chooses
what to read. The one-shot director is handed one digest and must decide from
it. This agent gets a catalogue and six read-only tools; which product to open,
which metric to compare and whether a review exists at all are its calls.

What stays in code, because a model must not be able to skip it:

* the tool list (read-only, filtered by the gateway),
* the evidence rule (>= 2 observations per pick, and a memory hit is not one),
* the task-order contract (same validator the one-shot path uses),
* the quota and the budgets.

The output is the same ``task_orders.json`` shape, plus ``evidence_refs`` and
``planner="agent"``. Downstream (workshop, review, publish) is untouched.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

from ..contracts import MAX_WINDOW_DAYS, validate_task_order
from ..llm import LLMClient, LLMError, TraceRecorder
from ..memory import MemoryStore
from ..quota import QuotaPolicy
from ..runtime import (
    AgentPolicy,
    Budget,
    EventStore,
    EvidenceLedger,
    Runtime,
    ToolGateway,
)
from ..runtime.loop import TERMINAL_STATUSES
from ..site_platform import SiteProduct
from ..store_metrics import MARGIN_FLOOR, entry_variant
from ..tools import (
    SEED_PAGE_ROWS,
    SEED_ROW_FIELDS,
    ToolContext,
    benchmark_doc,
    build_research_registry,
    category_summary,
    seed_row,
)
from .director import (
    BAND_CEIL_RATIO,
    BAND_FLOOR_RATIO,
    new_run_id,
    normalize_sku,
    normalize_store_health,
)

SYSTEM_PROMPT = """You are the research agent of a B2B export store. You decide which products to change this week.

You have tools. Verify before you assert.
1. Start from the catalogue rows in the seed. Open only products you are actually considering.
2. Every claim about a product needs an observation. Cite obs ids in evidence_refs.
3. Each pick needs >=2 evidence_refs from get_metrics / get_reviews / get_benchmarks / price_calc.
4. Never invent products, numbers, certifications or review text.
5. If nothing deserves a change this week, call decline with the reason and at least one observation that supports it.
6. Money is integer cents. Price band must be >= cost_floor, and between quota.price_band_low_ratio and quota.price_band_high_ratio times the current price.
7. variants >= variant_low, <= 5, window 7..window_high days, min_lift in (0, 1].
8. Finish with submit_task_orders or decline. Never reply with prose only.
9. The seed lists one page of the catalogue, not all of it. If a category looks suspicious, call list_products to see its worst rows. A product you cannot see is not a healthy product.
10. quota.may_select_low is the usual week for a store this size, not a quota to fill. Fewer picks than that is allowed when fewer products are genuinely broken: say why in underfill_reason. Never add a product just to reach a number."""

TERMINAL = {"submit_task_orders", "decline"}
# Which observations may be cited as evidence. A memory hit is context: it says
# what happened last month, not what this product looks like today.
EVIDENCE_TOOLS = frozenset(
    {"get_product", "get_metrics", "get_reviews", "get_benchmarks", "price_calc"}
)

# Four dimensions, all capped. 20 tool calls is enough for five picks with three
# lookups each plus one retry; beyond that the model is browsing, not deciding.
RESEARCH_BUDGET = Budget(
    max_steps=16,
    max_tool_calls=20,
    max_total_tokens=60_000,
    max_wall_seconds=240.0,
    max_submit_attempts=3,
)

# A pick needs two independent observations, each from a product-facing tool.
# A decline needs one: the claim is smaller ("nothing here is worth changing"),
# but it still has to be something the agent actually looked at.
EVIDENCE_MIN_REFS = 2
DECLINE_MIN_REFS = 1
MAX_DECISION_REASON_CHARS = 80
# Five task orders with citations and prose do not fit in 4096.
RESEARCH_MAX_TOKENS = 8192

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "store_health": {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "summary": {"type": "string"},
                "weak_categories": {"type": "array", "items": {"type": "string"}},
                "top_problem": {"type": "string"},
            },
        },
        "selection_reason": {"type": "string"},
        "underfill_reason": {
            "type": "string",
            "description": "Required when selected has fewer items than quota.may_select_low.",
        },
        "selected": {
            "type": "array",
            "minItems": 1,
            # Overwritten per run by submit_schema(): a fixed 5 here silently
            # capped every store at five picks, including the ones whose own
            # quota allows twelve.
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "problem": {"type": "string"},
                    "problem_label": {"type": "string"},
                    "problem_detail": {"type": "string"},
                    "priority": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                    },
                    "hypothesis": {"type": "string"},
                    "action": {"type": "string"},
                    "success_metric": {"type": "string"},
                    "min_lift": {"type": "number"},
                    "window_days": {"type": "integer", "minimum": 7, "maximum": 56},
                    "price_low_cents": {"type": "integer"},
                    "price_high_cents": {"type": "integer"},
                    "price_reason": {"type": "string"},
                    "variant_min": {"type": "integer", "minimum": 2, "maximum": 5},
                    "variant_max": {"type": "integer", "minimum": 2, "maximum": 5},
                    "variant_reason": {"type": "string"},
                    "must_include": {"type": "array", "items": {"type": "string"}},
                    "forbidden": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "product_id",
                    "problem",
                    "problem_detail",
                    "evidence",
                    "evidence_refs",
                    "hypothesis",
                    "success_metric",
                    "min_lift",
                    "window_days",
                    "price_low_cents",
                    "price_high_cents",
                    "variant_min",
                    "variant_max",
                ],
                "additionalProperties": True,
            },
        },
    },
    "required": ["selected"],
    "additionalProperties": True,
}

DECLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        # Advertised so the model fills it in first time; the real check is in
        # ``decline`` and cannot be skipped by ignoring the schema.
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["reason", "evidence_refs"],
    "additionalProperties": True,
}


def submit_schema(max_items: int) -> dict:
    """The submit contract with this store's actual ceiling.

    The ceiling used to be the literal 5 in ``SUBMIT_SCHEMA``, enforced by the
    tool gateway, so a 120-SKU store whose quota allows 12 could never submit
    more than five — and nothing told the model why. Generated per run so the
    seed, the schema and the check in ``submit_task_orders`` cannot disagree.
    """
    schema = copy.deepcopy(SUBMIT_SCHEMA)
    schema["properties"]["selected"]["maxItems"] = max(1, int(max_items))
    return schema


class ResearchError(Exception):
    """The agent run did not produce a usable task order. Never falls back."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("research: " + "; ".join(self.errors[:5]))


# Two ways to end a run badly that used to look identical: reaching for a tool
# you were not given is a security event, inventing tool names is a competence
# event. The hint tells the operator which one they are looking at.
_STATUS_HINTS = {
    "policy_violation": "the model kept calling tools it was not permitted to use",
    "tool_misuse": "the model kept calling tool names that do not exist",
    "budget_exhausted": "token / tool-call / wall-clock budget ran out mid-run",
    "step_limit": "the step cap was reached before a terminal tool was called",
    "no_tool_call": "the model replied with prose instead of a terminal tool",
    "invalid_task_orders": "the task orders kept failing contract or evidence checks",
}


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


def _traffic(product: SiteProduct) -> int:
    metrics = getattr(product, "metrics", None)
    return int(metrics.pv) if metrics else 0


def _memory_digest(memory: MemoryStore | None) -> dict:
    """Just enough to avoid re-deciding last week, and nothing that can be looked up.

    The prose summary and the full decision text used to ride along here. They
    are the two largest fields and the two least read: an agent that wants them
    can call ``search_memory``. Keeping the digest under ~250 characters is what
    lets twenty catalogue rows fit in the 3k seed budget.
    """
    if memory is None:
        return {"profile": {}, "open_experiments": 0, "lessons": 0, "last_decisions": []}
    profile = memory.read_profile() or {}
    health = profile.get("store_health") if isinstance(profile.get("store_health"), dict) else {}
    experiments = memory.read_experiments(limit=200, include_open=True)
    return {
        "profile": {
            "health_score": health.get("score"),
            "weak_categories": (health.get("weak_categories") or [])[:3],
            "active_products": profile.get("active_products"),
            "last_selected_count": profile.get("last_selected_count"),
        },
        "open_experiments": sum(
            1 for item in experiments if str(item.get("status")) == "observing"
        ),
        "lessons": len(memory.read_lessons() or []),
        "last_decisions": [
            {
                "at": item.get("at"),
                "moved": item.get("moved", item.get("selected_count")),
                "reason": str(item.get("reason") or "")[:MAX_DECISION_REASON_CHARS],
            }
            for item in (memory.read_decisions(limit=1) or [])
        ],
    }


def build_seed(
    *,
    store,
    products: list[SiteProduct],
    memory: MemoryStore | None,
    policy: QuotaPolicy,
    days: int = 30,
    limit_override: int = 0,
    window_max_days: int = MAX_WINDOW_DAYS,
    band_floor_ratio: float = BAND_FLOOR_RATIO,
    band_ceil_ratio: float = BAND_CEIL_RATIO,
) -> dict:
    """One page of the catalogue plus the facts needed to read it.

    Twenty rows, worst traffic first: opening this page should show the products
    most likely to need a change, not a random slice. The page is not the
    catalogue, and ``catalogue_note`` says so in as many words — otherwise "not
    listed" reads as "healthy", which is the failure mode this replaces.
    """
    low, high = policy.sku_bounds(len(products), limit_override)
    variant_low, variant_high = policy.variant_bounds(max(1, high))
    total = len(products)
    page = sorted(products, key=_traffic)[:SEED_PAGE_ROWS]
    seed = {
        "store": {
            "slug": str(getattr(store, "slug", "")),
            # The snapshot carries the seller's country, not the target market.
            # Calling it "market" would tell the model the store sells to CN.
            "country": str(getattr(store, "country", "") or ""),
            "currency": str(getattr(store, "currency", "USD") or "USD"),
        },
        "window_days": days,
        "catalogue_size": total,
        "quota": {
            "may_select_low": low,
            "may_select_high": high,
            "may_select_note": "low is the usual week for a store this size, not a quota to fill",
            "variant_low": variant_low,
            "variant_high": variant_high,
            "window_high": window_max_days,
            "price_band_low_ratio": band_floor_ratio,
            "price_band_high_ratio": band_ceil_ratio,
        },
        "categories": category_summary(products),
        # Row order matters: the catalogue is a table, so name the columns once.
        "catalogue_fields": list(SEED_ROW_FIELDS),
        "catalogue": [seed_row(product, days) for product in page],
        "memory_digest": _memory_digest(memory),
    }
    if len(page) < total:
        pages = (total + SEED_PAGE_ROWS - 1) // SEED_PAGE_ROWS
        seed["catalogue_note"] = (
            f"page 1 of {pages} ({len(page)} of {total} active products), pv ascending. "
            "Call list_products for the rest: an unseen row is not a healthy row."
        )
    return seed


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def evidence_errors(sku: dict, ledger: EvidenceLedger, *, minimum: int = EVIDENCE_MIN_REFS) -> list[str]:
    """At least ``minimum`` real observations, each from a product-facing tool.

    A pick needs two; a decline needs one (``DECLINE_MIN_REFS``). The rule is
    the same rule, only the bar moves, so submit and decline cannot drift apart.

    This lives here and not in ``contracts.py`` on purpose: the contract cannot
    know what a ledger is, and the workshop still has to read old task orders
    that have no evidence ids at all.
    """
    errors: list[str] = []
    product_id = str(sku.get("product_id") or "")
    refs = [str(item) for item in (sku.get("evidence_refs") or [])]
    # Citing obs-1 twice is one observation, not two pieces of evidence.
    unique = list(dict.fromkeys(refs))
    if len(unique) < minimum:
        plural = "s" if minimum != 1 else ""
        errors.append(f"{product_id}: need at least {minimum} evidence_ref{plural}")
    for ref in unique:
        item = ledger.get(ref)
        if item is None:
            errors.append(f"{product_id}: unknown observation {ref}")
        elif ledger.tool_of(ref) not in EVIDENCE_TOOLS:
            errors.append(f"{product_id}: {ref} is context, not evidence")
        elif not (item.get("result") or {}).get("ok"):
            # A 404 is a fact about the store, but it is not evidence about a
            # product's metrics, and it must not be cited as if it were.
            errors.append(f"{product_id}: {ref} is a failed call, not evidence")
    return errors


def inspected_product_ids(ledger: EvidenceLedger) -> list[str]:
    """Which products the agent actually opened, in first-seen order.

    Read from the observation payloads, not from the call arguments: a 404 left
    no payload, so it inspected nothing. A list result carries no single
    ``product_id`` either — seeing a row in a table is not opening the product.
    """
    seen: list[str] = []
    items = (ledger.to_dict() or {}).get("items") or {}
    for obs_id in items:
        item = ledger.get(obs_id) or {}
        payload = item.get("full_result") or item.get("result") or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        product_id = str(data.get("product_id") or "")
        if product_id and product_id not in seen:
            seen.append(product_id)
    return seen


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run_research(
    *,
    store,
    products: list[SiteProduct],
    client: LLMClient,
    memory: MemoryStore | None,
    policy: QuotaPolicy,
    run_id: str = "",
    trace: TraceRecorder | None = None,
    reviews: dict[str, list[dict]] | None = None,
    days: int = 30,
    budget: Budget | None = None,
    events: EventStore | None = None,
    limit_override: int = 0,
    margin_floor: float = MARGIN_FLOOR,
    window_max_days: int = MAX_WINDOW_DAYS,
    band_floor_ratio: float = BAND_FLOOR_RATIO,
    band_ceil_ratio: float = BAND_CEIL_RATIO,
) -> dict:
    """Decide the week with tools. Raises ResearchError instead of guessing."""
    if not client.enabled:
        raise LLMError(f"model not configured: {client.settings.describe()}")

    policy = policy or QuotaPolicy.from_env()
    run_id = run_id or new_run_id()
    active = [product for product in products if getattr(product, "status", "") == "active"]
    if not active:
        raise ResearchError(["没有在售商品，agent 模式无法研究"])
    products_by_id = {str(product.product_id): product for product in active}

    # Freeze what the model could see before it starts reading. The ledger, not
    # this file, records what it actually read.
    if memory is not None:
        memory.snapshot(run_id)

    seed = build_seed(
        store=store,
        products=active,
        memory=memory,
        policy=policy,
        days=days,
        limit_override=limit_override,
        window_max_days=window_max_days,
        band_floor_ratio=band_floor_ratio,
        band_ceil_ratio=band_ceil_ratio,
    )
    low, high = policy.sku_bounds(len(active), limit_override)
    variant_low, variant_high = policy.variant_bounds(max(1, high))

    ledger = EvidenceLedger()
    registry = build_research_registry()
    context = ToolContext(
        products=active,
        memory=memory,
        benchmarks=benchmark_doc(active),
        reviews=reviews or {},
    )
    gateway = ToolGateway(registry, AgentPolicy(role="research"), ledger)
    runtime = Runtime(
        client,
        gateway,
        ledger,
        events or EventStore(),
        budget=budget or RESEARCH_BUDGET,
        ctx=context,
        trace=trace,
        max_tokens=RESEARCH_MAX_TOKENS,
    )

    def build_document(
        payload: dict,
        selected: list[dict],
        warnings: list[str],
        *,
        notes: list[str] | None = None,
        status: str = "",
    ) -> dict:
        """The same envelope ``DirectorResult.as_payload()`` produces."""
        raw_health = payload.get("store_health")
        health = (
            normalize_store_health(raw_health)
            if isinstance(raw_health, dict) and raw_health
            else {
                # A fabricated 50 would look like a measurement. The agent can
                # still submit a real score; without one the console says so.
                "score": None,
                "summary": "agent 模式未单列体检；结论见每个任务的 evidence_refs",
                "weak_categories": [],
                "top_problem": "",
            }
        )
        below_floor = 0 < len(selected) < low
        underfill_reason = str(payload.get("underfill_reason") or "").strip()
        document = {
            "store": str(getattr(store, "slug", "")),
            "run_id": run_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "scanned": len(active),
            # How many distinct products were actually opened, as opposed to how
            # many were scanned. The gap between the two is the agent's work.
            "inspected": len(inspected_product_ids(ledger)),
            "matched": len(selected),
            "selected": selected,
            "store_health": health,
            "quota": {
                "value": len(selected),
                "low": low,
                "high": high,
                "requested": len(selected),
                # Not a clamp: the agent picked fewer than the usual week and we
                # did not invent work to fill the gap. Reported as underfill, not
                # as a trim — a trim means capacity was exceeded, which is the
                # opposite problem and must keep its own KPI.
                "guarded": False,
                "underfill": below_floor,
                "underfill_reason": underfill_reason,
                "reason": f"below usual floor {low}" if below_floor else "agent chose within bounds",
            },
            "underfill_reason": underfill_reason,
            "notes": notes
            if notes is not None
            else [
                f"扫描 {len(active)} 个在售商品；agent 用 "
                f"{gateway.executed} 次工具观察后决定改 {len(selected)} 个。"
            ],
            "violations": warnings,
            "repair_issues": [],
            "planner": "agent",
            "model": client.settings.model,
        }
        # The loop reads ``status`` to tell a decline from a submit. It is
        # dropped again before the file is written: downstream has never seen
        # that key in a task order and does not expect it.
        if status:
            document["status"] = status
        return document

    def submit_task_orders(arguments: dict):
        """Submit this week's task orders: 1..may_select_high products, each with >=2 evidence_refs."""
        payload = arguments if isinstance(arguments, dict) else {}
        raw_skus = payload.get("selected")
        if not isinstance(raw_skus, list) or not raw_skus:
            return (
                False,
                None,
                ["selected must be a non-empty list; call decline if nothing deserves a change"],
            )
        if len(raw_skus) > high:
            return False, None, [f"selected has {len(raw_skus)} items; quota allows at most {high}"]
        # Fewer than the usual week is allowed; saying why is not optional. The
        # alternative — silently accepting the smaller number — gives the model
        # no reason to explain, and the reviewer no way to tell a quiet week
        # from a lazy one.
        if len(raw_skus) < low and not str(payload.get("underfill_reason") or "").strip():
            return False, None, [
                f"selected has {len(raw_skus)} items, below the usual {low}; "
                "add underfill_reason explaining why this week is smaller "
                "(do not add a weak product to reach the number)"
            ]

        documents: list[dict] = []
        errors: list[str] = []
        warnings: list[str] = []
        for index, raw in enumerate(raw_skus):
            if not isinstance(raw, dict):
                errors.append(f"selected[{index}] is not an object")
                continue
            product_id = str(raw.get("product_id") or "").strip()
            product = products_by_id.get(product_id)
            if product is None:
                errors.append(
                    f"selected[{index}]: unknown product {product_id!r}; use ids from the catalogue"
                )
                continue
            if entry_variant(product) is None:
                # No variant means no price to move: nothing here is executable.
                errors.append(f"{product_id}: product has no variant, nothing to change")
                continue
            document, issues = normalize_sku(
                raw,
                product,
                run_id=run_id,
                variant_low=variant_low,
                variant_high=variant_high,
                margin_floor=margin_floor,
                window_max_days=window_max_days,
                band_floor_ratio=band_floor_ratio,
                band_ceil_ratio=band_ceil_ratio,
            )
            if document is None:
                errors.extend(issues or [f"{product_id}: task order is not executable"])
                continue
            warnings.extend(issues)
            document["evidence_refs"] = [str(item) for item in (raw.get("evidence_refs") or [])]
            document["planner"] = "agent"
            if isinstance(document.get("strategy"), dict):
                document["strategy"]["planner"] = "agent"
            # Contract before evidence: a malformed order with perfect citations
            # is still not executable, and the model can fix both in one retry.
            errors.extend(validate_task_order(document, max_window_days=window_max_days))
            errors.extend(evidence_errors(document, ledger))
            documents.append(document)

        if errors:
            return False, None, errors[:12]
        return True, build_document(payload, documents, warnings), []

    def decline(arguments: dict):
        """Change nothing this week and say why, with an observation behind it."""
        payload = arguments if isinstance(arguments, dict) else {}
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            return False, None, ["decline needs a reason"]
        refs = [str(item) for item in (payload.get("evidence_refs") or [])]
        # "Nothing is worth changing" is a claim about the store. Making it
        # without opening a single product is the cheapest way to look diligent
        # while doing no work, so the bar is one real observation plus one real
        # call — lower than a pick, but not zero.
        if int(getattr(gateway, "executed", 0)) <= 0:
            return False, None, [
                "decline rejected: no product was inspected; "
                "open at least one with get_metrics or get_product first"
            ]
        proof = evidence_errors(
            {"product_id": "decline", "evidence_refs": refs},
            ledger,
            minimum=DECLINE_MIN_REFS,
        )
        if proof:
            return False, None, proof
        notes = [f"本周不动：{reason}"]
        product_ids = inspected_product_ids(ledger)
        if memory is not None:
            try:
                memory.append_decision(
                    {
                        "decision_id": run_id,
                        "run_id": run_id,
                        "store_slug": str(getattr(store, "slug", "")),
                        "moved": 0,
                        "reason": reason,
                        "evidence_refs": refs,
                        # Two different questions, two different numbers: how
                        # many calls were made, and which products they were about.
                        "inspected": int(getattr(gateway, "executed", 0)),
                        "product_ids": product_ids,
                        "model": client.settings.model,
                    }
                )
            except OSError as exc:  # a broken disk must not block the decision
                notes.append(f"决策日志写入失败：{exc}")
        document = build_document(payload, [], [], notes=notes, status="declined")
        return True, document, []

    result = runtime.run(
        run_id=run_id,
        system_prompt=SYSTEM_PROMPT,
        seed=seed,
        terminal={
            "submit_task_orders": (submit_schema(high), submit_task_orders),
            "decline": (DECLINE_SCHEMA, decline),
        },
    )

    if result.status not in TERMINAL_STATUSES:
        raise ResearchError(
            [
                f"agent run stopped with status={result.status}"
                + (f": {_STATUS_HINTS[result.status]}" if result.status in _STATUS_HINTS else "")
                + "; no task order was written"
            ]
        )
    if not isinstance(result.payload, dict):
        raise ResearchError([f"agent run returned no payload (status={result.status})"])
    payload = dict(result.payload)
    # ``status`` is how the loop knows a decline from a submit; it is not part
    # of the task-order file and would confuse the console.
    payload.pop("status", None)
    return payload
