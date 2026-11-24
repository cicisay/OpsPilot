"""把已判定的实验归纳成经验库条目。

``experiment.judge()`` 已经把"这次到底有没有用"算清楚了；这里只做一件模型擅长
的事：从多条结论里看出**可复用的规律**，而不是复述单个商品。

两条防线，防止模型把经验库写成小说：

* **数字必须来自证据。** 条目里出现的百分数（``12%``）必须能在输入实验的
  lift / 基线 / 实测 / 目标里找到对应值，容差 0.05 个百分点；找不到就丢弃该条。
* **空结果不覆盖旧经验。** 模型返回空列表或全部条目都被校验丢弃时，保留磁盘上
  原有的 ``lessons.json``，宁可这一轮没学到东西，也不能把历史经验清零。

与 FR-510 一致：模型不可用时直接报错，不产出规则兜底的经验。
"""

from __future__ import annotations

import hashlib
import json
import re

from .llm.client import LLMClient, LLMError, parse_json_object
from .llm.trace import TraceRecorder

LESSON_MAX = 200
LESSON_MAX_TOKENS = 3072
STATEMENT_MAX = 200
SCOPE_MAX = 60
DIRECTIONS = ("worked", "failed", "mixed")

SYSTEM_PROMPT = """You curate a cross-border e-commerce operations team's lesson book.

You receive (a) experiments that already have a verdict, and (b) the lessons that
are already stored. Turn the finished experiments into reusable lessons.

Rules:
- One lesson = one rule of thumb that would change what the team does next time.
  "Add the lid type to the title and the order file" is a lesson.
  "Product X improved" is a report, not a lesson.
- scope is a short label, never a sentence: the category slug ("home-kitchen"),
  a copy lever as "copy:<lever>" ("copy:certification", "copy:lead-time"), or
  "store" when the finding is global. Do not paste the angle text into scope.
- direction: "worked" (do more of it), "failed" (stop doing it), "mixed".
- confidence 0..1. A single experiment is weak evidence: cap it at 0.4. Raise it
  only when several experiments point the same way.
- evidence_count = how many experiments support this lesson.
- Quote the measured lift when it makes the lesson concrete ("订单率提升 34%").
  Never invent numbers: any percentage you write must appear in the evidence.
- Merge with the stored lessons instead of duplicating them; keep the useful old
  ones. Return the complete list (at most 200).
- Write statements in Chinese, one sentence, no product names.

Reply with JSON only:
{"lessons": [{"scope": "...", "statement": "...", "direction": "worked",
              "evidence_count": 2, "confidence": 0.5}]}"""

_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


class LessonError(Exception):
    """模型没给出可用经验。调用方据此中止，而不是写一份假的。"""


def _percent_values(text: str) -> list[float]:
    return [float(match) for match in _PERCENT.findall(str(text or ""))]


def _evidence_percents(records: list[dict]) -> list[float]:
    """Every percentage the model is allowed to quote.

    Rates are stored as fractions (0.0012) but written as percentages (0.12%),
    and lifts are fractions too, so both are scaled by 100. Magnitudes are
    compared, so a −12% lift grounds "下降 12%" as well.
    """
    values: list[float] = []
    for record in records:
        outcome = record.get("outcome") or {}
        for key in ("lift", "min_lift"):
            raw = outcome.get(key)
            if raw is None:
                continue
            try:
                values.append(abs(float(raw) * 100))
            except (TypeError, ValueError):
                continue
        for block in (outcome.get("baseline"), outcome.get("treated")):
            if not isinstance(block, dict):
                continue
            for key in ("order_rate", "click_rate", "return_rate"):
                raw = block.get(key)
                if raw is None:
                    continue
                try:
                    values.append(abs(float(raw) * 100))
                except (TypeError, ValueError):
                    continue
    # Anything said in the source fields is fair game too: "降价 8%" comes from
    # the proposal angle, not from the outcome, and is a fact rather than a
    # hallucination.
    for record in records:
        for key in ("success_metric", "angle", "hypothesis", "problem"):
            values.extend(_percent_values(record.get(key) or ""))
    return values


def _number_is_grounded(statement: str, allowed: list[float]) -> bool:
    for value in _percent_values(statement):
        if not any(abs(value - candidate) <= 0.05 for candidate in allowed):
            return False
    return True


def lesson_id(scope: str, statement: str) -> str:
    digest = hashlib.sha1(f"{scope}|{statement}".encode("utf-8")).hexdigest()
    return f"L{digest[:10]}"


def build_payload(records: list[dict], existing: list[dict]) -> dict:
    """Only what the lesson writer needs: verdicts, numbers, and old lessons."""
    return {
        "experiments": [
            {
                "product_id": record.get("product_id"),
                "category": record.get("category"),
                "angle": record.get("angle"),
                "hypothesis": record.get("hypothesis"),
                "problem": record.get("problem"),
                "success_metric": record.get("success_metric"),
                "min_lift": (record.get("outcome") or {}).get("min_lift"),
                "verdict": record.get("verdict"),
                "verdict_cn": record.get("verdict_cn"),
                "lift": (record.get("outcome") or {}).get("lift"),
                "p_value": (record.get("outcome") or {}).get("p_value"),
                "baseline": (record.get("outcome") or {}).get("baseline"),
                "treated": (record.get("outcome") or {}).get("treated"),
                "note": (record.get("outcome") or {}).get("note"),
            }
            for record in records
        ],
        "existing_lessons": [
            {
                "scope": item.get("scope"),
                "statement": item.get("statement"),
                "direction": item.get("direction"),
                "evidence_count": item.get("evidence_count"),
                "confidence": item.get("confidence"),
            }
            for item in existing
        ],
        "instruction": (
            "Return the complete merged lesson list. Ground every percentage in the "
            "evidence above. At most 200 lessons."
        ),
    }


def normalize_lessons(payload, records: list[dict], issues: list[str] | None = None) -> list[dict]:
    """Validate and de-duplicate what the model wrote; drop anything ungrounded."""
    issues = issues if issues is not None else []
    items = payload.get("lessons") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        issues.append("reply has no lessons array")
        return []

    allowed = _evidence_percents(records)
    seen: set[tuple[str, str]] = set()
    lessons: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        scope = str(raw.get("scope") or "").strip()[:SCOPE_MAX]
        statement = str(raw.get("statement") or "").strip().replace("\n", " ")
        if not scope or not statement:
            issues.append("lesson without scope or statement dropped")
            continue
        if len(statement) > STATEMENT_MAX:
            statement = statement[:STATEMENT_MAX].rstrip()
        direction = str(raw.get("direction") or "").strip().lower()
        if direction not in DIRECTIONS:
            direction = "mixed"
        if not _number_is_grounded(statement, allowed):
            issues.append(f"ungrounded percentage dropped: {statement[:40]}")
            continue
        key = (scope.lower(), statement.lower())
        if key in seen:
            continue
        seen.add(key)
        try:
            evidence = max(1, int(raw.get("evidence_count") or 1))
        except (TypeError, ValueError):
            evidence = 1
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.3
        confidence = max(0.0, min(1.0, confidence))
        # One experiment cannot justify high confidence, whatever the model says.
        if evidence <= 1:
            confidence = min(confidence, 0.4)
        lessons.append(
            {
                "lesson_id": lesson_id(scope, statement),
                "scope": scope,
                "statement": statement,
                "direction": direction,
                "evidence_count": evidence,
                "confidence": round(confidence, 2),
            }
        )
    return lessons[:LESSON_MAX]


def derive_lessons(
    client: LLMClient,
    records: list[dict],
    existing: list[dict],
    trace: TraceRecorder | None = None,
) -> tuple[list[dict], str, list[str]]:
    """One model call. Returns ``(lessons, model, issues)``.

    Raises ``LessonError`` when the model is unavailable or unparsable, so the
    caller can report "经验库本轮未更新" instead of silently keeping stale data
    or writing an empty file.
    """
    if not client.enabled:
        raise LessonError(f"model not configured: {client.settings.describe()}")
    if not records:
        raise LessonError("没有已判定的实验，无法归纳经验")

    payload = build_payload(records, existing)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    reply = None
    try:
        reply = client.chat(messages, temperature=0.2, max_tokens=LESSON_MAX_TOKENS)
        data = parse_json_object(reply.text)
    except LLMError as exc:
        if trace is not None:
            trace.record(
                agent="lessons",
                ok=False,
                model=client.settings.model,
                messages=messages,
                response=reply.text if reply is not None else "",
                error=str(exc),
                meta={"agent": "lessons", "records": len(records)},
            )
        raise LessonError(str(exc)) from None

    issues: list[str] = []
    lessons = normalize_lessons(data, records, issues)
    if not lessons:
        issues.append("模型未产出可用经验，保留原经验库")
    if trace is not None:
        trace.record(
            agent="lessons",
            ok=True,
            model=reply.model,
            messages=messages,
            response=reply.text,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            latency_ms=reply.latency_ms,
            meta={"agent": "lessons", "records": len(records), "lessons": len(lessons)},
        )
    return lessons, reply.model, issues
