"""Contracts shared by the three agents.

Every hand-off between agents is a plain dict that must pass one of the
validators here before the next agent may read it. The model is allowed to be
creative; it is not allowed to be malformed.

Why dicts and not dataclasses: the existing pipeline already passes JSON-shaped
dicts between stages, and a dataclass round-trip would add conversion code
without buying anything. Validators are the single source of truth for shape.

Design rule: validators never raise on user data. They return a list of human
readable errors so the caller can decide whether to retry, drop one item, or
abort the batch.
"""

from __future__ import annotations

from typing import Any, Iterable

# Four compliance dimensions. Ordered from most to least common in practice.
CHECK_KEYS = ("c1_absolute", "c2_ip", "c3_category", "c4_market")

CHECK_LABELS = {
    "c1_absolute": "绝对化与承诺性表述",
    "c2_ip": "品牌与知识产权",
    "c3_category": "禁售类目与资质",
    "c4_market": "目标市场法规",
}

SEVERITY_ORDER = {"pass": 0, "warn": 1, "block": 2}

VERDICT_STATES = ("supported", "rejected", "inconclusive", "insufficient")
EXPERIMENT_STATUS = ("observing", "verdict")

MIN_VARIANTS = 2
MAX_VARIANTS = 5
MIN_EVIDENCE = 2
MIN_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 56


class ContractError(ValueError):
    """Raised when a payload is too broken to continue the batch."""

    def __init__(self, stage: str, errors: Iterable[str]) -> None:
        self.stage = stage
        self.errors = list(errors)
        super().__init__(f"{stage}: " + "; ".join(self.errors[:5]))


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_task_order(payload: dict) -> dict:
    """Coerce a model reply into the canonical TaskOrder shape.

    Missing keys are filled so validators can report every problem at once
    instead of failing on the first one.
    """
    payload = payload if isinstance(payload, dict) else {}
    low = _int(payload.get("price_low_cents"))
    high = _int(payload.get("price_high_cents"))
    if low and high and low > high:
        low, high = high, low
    points = [_int(item) for item in (payload.get("price_points") or []) if _int(item) > 0]
    variant_min = _int(payload.get("variant_min"), MIN_VARIANTS)
    variant_max = _int(payload.get("variant_max"), MIN_VARIANTS)
    if variant_min > variant_max:
        variant_min, variant_max = variant_max, variant_min
    document = {
        "task_id": _text(payload.get("task_id")),
        "run_id": _text(payload.get("run_id")),
        "product_id": _text(payload.get("product_id")),
        "title": _text(payload.get("title")),
        "category": _text(payload.get("category")),
        "problem": _text(payload.get("problem")),
        "problem_detail": _text(payload.get("problem_detail")),
        "evidence": _str_list(payload.get("evidence")),
        "hypothesis": _text(payload.get("hypothesis")),
        "success_metric": _text(payload.get("success_metric")),
        "min_lift": round(_float(payload.get("min_lift")), 4),
        "window_days": _int(payload.get("window_days"), 14),
        "price_low_cents": low,
        "price_high_cents": high,
        "price_points": points,
        "variant_min": variant_min,
        "variant_max": variant_max,
        "must_include": _str_list(payload.get("must_include")),
        "forbidden": _str_list(payload.get("forbidden")),
    }
    # Added for the tool-using director. Only passed through when the caller
    # supplied it, so a one-shot task order keeps exactly the keys it had.
    if "evidence_refs" in payload:
        document["evidence_refs"] = _str_list(payload.get("evidence_refs"))
    # Added for the intent layer (Step 3). Same rule as evidence_refs: absent
    # means absent, so the scheduled path keeps exactly the keys it had and the
    # contract stays a superset instead of a new shape.
    #
    # - source: where the order came from ("scheduled" / "intent")
    # - status: "draft" = 计划预览里的草案，没定稿、不能进工坊
    # - slots:  槽位与假设标记（assumed / pending_input），执行前用于确认
    # - intake: 委托溯源（原话摘要 / 意图 / 置信度），只做审计线索
    if "source" in payload:
        document["source"] = _text(payload.get("source"))
    if "status" in payload:
        # 大小写统一，避免 "Draft" 绕过下面的草稿判定。
        document["status"] = _text(payload.get("status")).lower()
    if "slots" in payload:
        slots = payload.get("slots")
        document["slots"] = dict(slots) if isinstance(slots, dict) else {}
    if "intake" in payload:
        intake = payload.get("intake")
        document["intake"] = dict(intake) if isinstance(intake, dict) else {}
    return document


def validate_task_order(doc: dict, *, max_window_days: int = MAX_WINDOW_DAYS) -> list[str]:
    """A task must be executable without asking the model a follow-up question.

    One exemption: ``status == "draft"``. A draft is a plan-preview artifact —
    it has no evidence yet by definition, and pretending it does would be worse
    than admitting it. Everything else (price band, variant range, window,
    problem_detail) still has to be valid, so a draft cannot be a shape-free
    escape hatch.

    ``max_window_days`` is the store's own verification cadence: a supplier whose
    buyers decide in a quarter cannot be judged on a 14-day window. It is a
    keyword so every existing caller keeps the PRD default.
    """
    errors: list[str] = []
    ceiling = max(MIN_WINDOW_DAYS, int(max_window_days or MAX_WINDOW_DAYS))
    status = _text(doc.get("status")).lower()
    for key in ("task_id", "product_id", "problem", "hypothesis", "success_metric"):
        if not _text(doc.get(key)):
            errors.append(f"missing {key}")
    if not _text(doc.get("problem_detail")):
        errors.append("missing problem_detail: a problem label alone is not actionable")
    if status != "draft" and len(doc.get("evidence") or []) < MIN_EVIDENCE:
        errors.append(f"evidence needs >= {MIN_EVIDENCE} items, got {len(doc.get('evidence') or [])}")
    if not (0 < _float(doc.get("min_lift")) <= 1):
        errors.append("min_lift must be in (0, 1]")
    window = _int(doc.get("window_days"))
    if not (MIN_WINDOW_DAYS <= window <= ceiling):
        errors.append(f"window_days must be {MIN_WINDOW_DAYS}..{ceiling}, got {window}")
    low = _int(doc.get("price_low_cents"))
    high = _int(doc.get("price_high_cents"))
    if low <= 0 or high <= 0:
        errors.append("price band must be positive")
    elif low > high:
        errors.append("price_low_cents must not exceed price_high_cents")
    points = doc.get("price_points") or []
    if not points:
        errors.append("price_points must not be empty")
    else:
        outside = [p for p in points if not (low <= _int(p) <= high)]
        if outside:
            errors.append(f"price_points outside band: {outside}")
        if len(set(_int(p) for p in points)) != len(points):
            errors.append("price_points contains duplicates")
    variant_min = _int(doc.get("variant_min"))
    variant_max = _int(doc.get("variant_max"))
    if not (MIN_VARIANTS <= variant_min <= variant_max <= MAX_VARIANTS):
        errors.append(f"variant range must sit inside {MIN_VARIANTS}..{MAX_VARIANTS}")
    # Shape only. Whether an observation exists is a runtime fact, checked by
    # the agent that owns the ledger; the contract has no ledger to ask.
    refs = doc.get("evidence_refs")
    if refs is not None:
        if not isinstance(refs, (list, tuple)):
            errors.append("evidence_refs must be a list of observation ids")
        else:
            bad = [item for item in refs if not isinstance(item, str)]
            if bad:
                errors.append(f"evidence_refs must be strings, got {len(bad)} non-string")
    return errors


def normalize_variant(payload: dict, index: int = 0) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    bullets = _str_list(payload.get("bullets"))
    self_check = payload.get("self_check")
    return {
        # variant_id is what the contract names; variant_key is what the site,
        # the review file and the publish path have always used. Both are kept
        # in sync so a proposal can travel through every layer without a lookup.
        "variant_id": _text(payload.get("variant_id") or payload.get("variant_key")),
        "variant_key": _text(payload.get("variant_key") or payload.get("variant_id")),
        "index": _int(payload.get("index"), index),
        "angle": _text(payload.get("angle")),
        "title": _text(payload.get("title")),
        "bullets": bullets,
        "description": _text(payload.get("description")),
        "price_index": _int(payload.get("price_index"), -1),
        "price_cents": _int(payload.get("price_cents")),
        "material_brief": _str_list(payload.get("material_brief")),
        "self_review": _text(payload.get("self_review")),
        "self_check": self_check if isinstance(self_check, dict) else {},
    }


def normalize_proposal(payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    raw_variants = payload.get("variants") or []
    variants = [
        normalize_variant(item, index)
        for index, item in enumerate(raw_variants if isinstance(raw_variants, list) else [])
    ]
    return {
        "task_id": _text(payload.get("task_id")),
        "product_id": _text(payload.get("product_id")),
        "variant_count": _int(payload.get("variant_count"), len(variants)),
        "variant_reason": _text(payload.get("variant_reason")),
        "variants": variants,
    }


def validate_proposal(doc: dict, task: dict | None = None) -> list[str]:
    """A proposal is only acceptable if every variant can be reviewed standalone."""
    errors: list[str] = []
    if not _text(doc.get("task_id")):
        errors.append("missing task_id")
    variants = doc.get("variants") or []
    if not variants:
        errors.append("no variants produced")
        return errors
    declared = _int(doc.get("variant_count"), len(variants))
    if declared != len(variants):
        errors.append(f"variant_count {declared} does not match {len(variants)} variants")
    if not (MIN_VARIANTS <= len(variants) <= MAX_VARIANTS):
        errors.append(f"variant count must be {MIN_VARIANTS}..{MAX_VARIANTS}, got {len(variants)}")
    if task:
        low = _int(task.get("variant_min"), MIN_VARIANTS)
        high = _int(task.get("variant_max"), MAX_VARIANTS)
        if not (low <= len(variants) <= high):
            errors.append(f"variant count {len(variants)} outside task range {low}..{high}")
    seen_ids: set[str] = set()
    seen_angles: set[str] = set()
    for index, variant in enumerate(variants):
        prefix = f"variant[{index}]"
        for key in ("angle", "title", "description"):
            if not _text(variant.get(key)):
                errors.append(f"{prefix}: missing {key}")
        bullets = variant.get("bullets") or []
        if not (2 <= len(bullets) <= 6):
            errors.append(f"{prefix}: bullets must be 2..6, got {len(bullets)}")
        if _int(variant.get("price_index"), -1) < 0:
            errors.append(f"{prefix}: price_index must be >= 0")
        angle = _text(variant.get("angle")).lower()
        if angle:
            if angle in seen_angles:
                errors.append(f"{prefix}: duplicate angle {angle}")
            seen_angles.add(angle)
        variant_id = _text(variant.get("variant_id"))
        if variant_id:
            if variant_id in seen_ids:
                errors.append(f"{prefix}: duplicate variant_id {variant_id}")
            seen_ids.add(variant_id)
    if len(variants) >= 2 and len(seen_angles) < len(variants):
        errors.append("variants are not differentiated: angles must be unique")
    return errors


def build_verdict(payload: dict) -> dict:
    """Compute severity instead of trusting the model with it.

    The model reports four independent checks. The worst one wins; a model that
    says ``severity=pass`` while one check is ``block`` is simply overruled.
    """
    payload = payload if isinstance(payload, dict) else {}
    checks: dict[str, str] = {}
    for key in CHECK_KEYS:
        raw = _text(payload.get(key) or (payload.get("checks") or {}).get(key)).lower()
        checks[key] = raw if raw in SEVERITY_ORDER else "warn"
    severity = max(checks.values(), key=lambda item: SEVERITY_ORDER[item])
    findings = []
    for item in payload.get("findings") or []:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                "category": _text(item.get("category")),
                "quote": _text(item.get("quote")),
                "rule": _text(item.get("rule")),
                "suggestion": _text(item.get("suggestion")),
            }
        )
    return {
        "variant_id": _text(payload.get("variant_id")),
        "checks": checks,
        "severity": severity,
        "findings": findings,
        "overridden_by": _text(payload.get("overridden_by")),
    }


def validate_verdict(doc: dict) -> list[str]:
    errors: list[str] = []
    if not _text(doc.get("variant_id")):
        errors.append("missing variant_id")
    checks = doc.get("checks") or {}
    missing = [key for key in CHECK_KEYS if key not in checks]
    if missing:
        errors.append(f"missing compliance checks: {missing}")
    for key, value in checks.items():
        if key in CHECK_KEYS and value not in SEVERITY_ORDER:
            errors.append(f"check {key} has invalid state {value!r}")
    severity = _text(doc.get("severity")).lower()
    if severity not in SEVERITY_ORDER:
        errors.append(f"invalid severity {severity!r}")
    elif checks:
        worst = max(checks.values(), key=lambda item: SEVERITY_ORDER.get(item, -1))
        # An unknown state already raised above; do not compare against it.
        if worst in SEVERITY_ORDER and SEVERITY_ORDER[severity] != SEVERITY_ORDER[worst]:
            errors.append(f"severity {severity} contradicts checks (worst is {worst})")
    for index, finding in enumerate(doc.get("findings") or []):
        for key in ("category", "quote", "rule", "suggestion"):
            if not _text(finding.get(key)):
                errors.append(f"finding[{index}]: missing {key}")
    if severity == "block" and not (doc.get("findings") or []):
        errors.append("block verdict must cite at least one finding")
    return errors


def normalize_experiment(payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    status = _text(payload.get("status")).lower() or "observing"
    verdict = _text(payload.get("verdict")).lower()
    return {
        "experiment_id": _text(payload.get("experiment_id")),
        "product_id": _text(payload.get("product_id")),
        "variant_id": _text(payload.get("variant_id")),
        "run_id": _text(payload.get("run_id")),
        "started_at": _text(payload.get("started_at")),
        "window_days": _int(payload.get("window_days"), 14),
        "baseline": payload.get("baseline") or {},
        "status": status if status in EXPERIMENT_STATUS else "observing",
        "verdict": verdict if verdict in VERDICT_STATES else "",
        "outcome": payload.get("outcome") or {},
    }


def validate_experiment(doc: dict) -> list[str]:
    errors: list[str] = []
    for key in ("experiment_id", "product_id", "variant_id", "started_at"):
        if not _text(doc.get(key)):
            errors.append(f"missing {key}")
    window = _int(doc.get("window_days"))
    if not (1 <= window <= MAX_WINDOW_DAYS):
        errors.append(f"window_days must be 1..{MAX_WINDOW_DAYS}, got {window}")
    status = _text(doc.get("status"))
    if status not in EXPERIMENT_STATUS:
        errors.append(f"invalid status {status!r}")
    verdict = _text(doc.get("verdict"))
    if status == "verdict" and verdict not in VERDICT_STATES:
        errors.append("a judged experiment needs one of " + ", ".join(VERDICT_STATES))
    if status == "observing" and verdict:
        errors.append("an observing experiment must not carry a verdict yet")
    if not doc.get("baseline"):
        errors.append("baseline is required: without it the verdict is not reproducible")
    return errors


def require(stage: str, errors: list[str]) -> None:
    """Abort the batch when a contract is broken beyond repair."""
    if errors:
        raise ContractError(stage, errors)
