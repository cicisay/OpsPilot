"""合规审查 Agent：四类检查，任一类 block 即否决。

三条边界，和旧的 ``reviewer.py`` 不同：

1. **四类都要有结论**。C1 绝对化、C2 知识产权、C3 禁售资质、C4 市场法规，
   缺一类就算未完成，不让"只查了绝对化"的方案混进审批队列。
2. **严重度由代码算**。模型只报四类的状态，``build_verdict`` 取最严的一类；
   模型自报 ``severity=pass`` 但某一类是 block 时，代码直接推翻。
3. **硬禁清单保留在代码里**（Q1 的结论）。禁售类目、他人品牌、版权形象命中时，
   即使模型判 pass 也强制 block，并记 ``overridden_by=code``。这不是业务判断，
   是法律底线；词库只作为证据，不参与"这条文案好不好"的判断。

没有拿到结论的版本不进入审批队列（fail-closed）：宁可少一个方案，也不放一个
没人审过的方案过去。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..compliance import ComplianceChecker
from ..contracts import (
    CHECK_KEYS,
    CHECK_LABELS,
    SEVERITY_ORDER,
    build_verdict,
)
from ..llm import LLMClient, LLMError, TraceRecorder, parse_json_object
from ..site_platform import SiteProduct

COMPLIANCE_MAX_TOKENS = 4096

# Rules that are not up for semantic debate. A trademark, a prohibited category
# or a copyrighted character is a legal stop, not a tone suggestion.
HARD_RULE_IDS = frozenset({"CAT-001", "BRD-001", "CPY-001"})

SEVERITY_CN = {"pass": "通过", "warn": "提示", "block": "否决"}

SYSTEM_PROMPT = """You are the compliance reviewer of a B2B export store.
You see finished listing variants, the product facts, the target market and the
hits our term list already flagged. You decide whether each variant may reach a
human approver.

Run four independent checks on every variant:
- c1_absolute: absolute or guaranteed claims (100%, guaranteed, best, No.1,
  cheapest, perfect, risk-free, cure, "zero failure", "包您满意" and paraphrases).
  A percentage that is a material composition ("100% cotton") stated in the
  product facts is not an absolute claim.
- c2_ip: third-party trademarks, brand names, celebrities, film/anime characters,
  logos in copy or in the image brief.
- c3_category: category requires certification (CE/RoHS/FCC) or is prohibited in
  the target market; the copy claims a certification the facts do not support.
- c4_market: market-specific wording (German "klimaneutral", unauthorised "TÜV",
  UK "risk-free"), local labelling and advertising rules.

Each check is "pass", "warn" or "block".
Rules:
- block: the variant must not be published as written.
- warn: publishable after a human reads the note.
- pass: nothing to fix.
- The term-list hits in lexicon_evidence are evidence, not verdicts. Judge the
  context: a brand word used as a compatibility statement is still c2_ip unless
  the facts explicitly authorise it.
- Every finding must quote the exact fragment, name the rule, and say how to fix
  it. "Not compliant" alone is not a finding.
- Never invent a certification, a law article or a test report.
- Judge only what is in the payload. Do not ask for more material.

Output JSON only, no markdown fence:
{
  "verdicts": [
    {
      "variant_key": "from the payload",
      "c1_absolute": "pass",
      "c2_ip": "pass",
      "c3_category": "warn",
      "c4_market": "pass",
      "findings": [
        {"category": "c1_absolute", "quote": "exact fragment", "rule": "why it is a problem", "suggestion": "how to fix"}
      ]
    }
  ]
}"""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def variant_text(variant: dict) -> str:
    return " ".join(
        [
            _text(variant.get("title")),
            *_str_list(variant.get("bullets")),
            _text(variant.get("description")),
        ]
    )


def brief_text(variant: dict) -> str:
    """The designer brief, whichever field carried it.

    ``workshop_agent`` writes ``material_brief`` (a 1-3 item list) and never
    fills ``image_brief``. Reading only the latter meant the brief was invisible
    to every lexicon rule, so "主图：nike logo 特写" passed as clean copy. Both
    fields are the same claim to a buyer, so both are checked.
    """

    parts = _str_list(variant.get("material_brief"))
    image = _text(variant.get("image_brief"))
    if image:
        parts.append(image)
    return " ".join(parts)


def lexicon_suspects(
    checker: ComplianceChecker,
    variants: list[dict],
    *,
    category: str,
    market: str,
) -> dict[str, list[dict]]:
    """Rule-engine hits, demoted to evidence for the model to judge."""
    suspects: dict[str, list[dict]] = {}
    for index, variant in enumerate(variants):
        key = _text(variant.get("variant_key")) or f"variant-{index}"
        report = checker.check(
            variant_text(variant),
            brief_text(variant),
            category,
            market,
        )
        hits = [
            {
                "rule_id": item.rule_id,
                "message": item.message,
                "suggestion": item.suggestion,
                "severity": item.severity,
                "quote": item.quote,
            }
            for item in report.violations
        ]
        if hits:
            suspects[key] = hits
    return suspects


def build_payload(
    proposal: dict,
    product: SiteProduct,
    *,
    market: str,
    suspects: dict[str, list[dict]],
) -> dict:
    variants = []
    for index, variant in enumerate(proposal.get("variants") or []):
        brief = _str_list(variant.get("material_brief"))
        image = _text(variant.get("image_brief"))
        if image and image not in brief:
            brief = brief + [image]
        variants.append(
            {
                "variant_key": _text(variant.get("variant_key")) or f"variant-{index}",
                "angle": _text(variant.get("angle")),
                "title": _text(variant.get("title")),
                "bullets": _str_list(variant.get("bullets")),
                "description": _text(variant.get("description")),
                "image_brief": brief,
                "price_cents": variant.get("price_cents"),
            }
        )
    return {
        "market": market,
        "category": getattr(product, "category_slug", "") or "",
        "product_facts": {
            "title": getattr(product, "title_en", ""),
            "moq_pcs": getattr(product, "moq", 0),
            "lead_time_days": getattr(product, "lead_time_days", 0),
            "incoterm": getattr(product, "incoterm", ""),
            "certifications": list(getattr(product, "certifications", ()) or ()),
        },
        "check_legend": CHECK_LABELS,
        "variants": variants,
        "lexicon_evidence": suspects,
        "instruction": (
            "Review every variant on all four checks. Cite the exact fragment for "
            "each finding. Reply with one verdict per variant_key."
        ),
    }


@dataclass
class ComplianceResult:
    product_id: str
    verdicts: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    calls: int = 0
    model: str = ""
    planner: str = "llm"

    @property
    def blocked(self) -> list[dict]:
        return [item for item in self.verdicts if item.get("severity") == "block"]

    @property
    def overridden(self) -> int:
        return sum(1 for item in self.verdicts if item.get("overridden_by") == "code")

    def as_payload(self) -> dict:
        return {
            "product_id": self.product_id,
            "verdicts": self.verdicts,
            "dropped": self.dropped,
            "issues": self.issues,
            "model": self.model,
            "planner": self.planner,
        }


class ComplianceError(Exception):
    """No variant could be certified. The proposal must not be queued."""

    def __init__(self, product_id: str, issues: list[str]) -> None:
        self.product_id = product_id
        self.issues = list(issues)
        super().__init__(f"compliance failed for {product_id}: " + "; ".join(self.issues[:4]))


class ComplianceAgent:
    role = "compliance"

    def __init__(self, client: LLMClient, trace: TraceRecorder | None = None) -> None:
        self.client = client
        self.trace = trace

    def review(self, payload: dict, *, repair_issues: list[str] | None = None) -> dict:
        data = dict(payload)
        if repair_issues:
            data["validation_issues"] = repair_issues[:12]
            data["instruction"] = (
                "Your previous reply was incomplete. Return one verdict per variant_key, "
                "with all four checks filled in. Do not skip any variant."
            )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ]
        try:
            reply = self.client.chat(
                messages,
                temperature=0.1,
                max_tokens=COMPLIANCE_MAX_TOKENS,
            )
        except LLMError as exc:
            self._record(messages, error=str(exc))
            raise
        try:
            parsed = parse_json_object(reply.text)
        except LLMError as exc:
            self._record(messages, reply=reply, error=str(exc))
            raise
        self._record(messages, reply=reply)
        return parsed

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


def _raw_checks(item: dict) -> dict:
    checks = item.get("checks")
    nested = checks if isinstance(checks, dict) else {}
    return {
        key: _text(item.get(key) or nested.get(key)).lower()
        for key in CHECK_KEYS
    }


def _missing_checks(item: dict) -> list[str]:
    checks = _raw_checks(item)
    return [key for key, value in checks.items() if value not in SEVERITY_ORDER]


def index_verdicts(raw: dict, variant_keys: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Map the model's verdicts back onto variants, by key or by position."""
    issues: list[str] = []
    indexed: dict[str, dict] = {}
    items = raw.get("verdicts")
    if not isinstance(items, list):
        return {}, ["verdicts is not a list"]
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(f"verdicts[{position}] is not an object")
            continue
        key = _text(item.get("variant_key") or item.get("variant_id"))
        if key not in variant_keys:
            if position < len(variant_keys):
                key = variant_keys[position]
            else:
                issues.append(f"verdicts[{position}]: unknown variant_key {key!r}, dropped")
                continue
        if key in indexed:
            issues.append(f"verdicts[{position}]: duplicate variant_key {key!r}, kept the first")
            continue
        indexed[key] = item
    return indexed, issues


def _hard_findings(suspects: list[dict]) -> list[dict]:
    findings = []
    for hit in suspects:
        if hit.get("rule_id") not in HARD_RULE_IDS:
            continue
        findings.append(
            {
                "category": "c3_category" if hit["rule_id"] == "CAT-001" else "c2_ip",
                "quote": str(hit.get("quote") or hit["rule_id"]),
                "rule": hit.get("message") or hit["rule_id"],
                "suggestion": hit.get("suggestion") or "删除相关表述后重做",
            }
        )
    return findings


def apply_hard_ban(verdict: dict, suspects: list[dict]) -> dict:
    """The model may pass a trademark; the code may not.

    Only the dimensions that actually caught something are marked ``block``.
    Painting all four red told the approver to rewrite absolute claims and
    market wording that the lexicon never complained about, which makes the
    four-check matrix useless at exactly the moment it matters.
    """

    if not any(hit.get("rule_id") in HARD_RULE_IDS for hit in suspects):
        return verdict
    hard = _hard_findings(suspects)
    existing = {
        (item.get("category"), item.get("rule")) for item in verdict.get("findings") or []
    }
    for finding in hard:
        if (finding["category"], finding["rule"]) not in existing:
            verdict.setdefault("findings", []).append(finding)
    checks = dict(_raw_checks(verdict))
    for finding in hard:
        checks[finding["category"]] = "block"
    verdict["checks"] = {key: checks.get(key, "pass") for key in CHECK_KEYS}
    verdict["severity"] = "block"
    verdict["overridden_by"] = "code"
    return verdict


def finalize_verdicts(
    raw: dict,
    *,
    variants: list[dict],
    suspects: dict[str, list[dict]],
) -> tuple[list[dict], list[dict], list[str]]:
    """Return accepted verdicts, dropped variants and what went wrong."""
    keys = [
        _text(variant.get("variant_key")) or f"variant-{index}"
        for index, variant in enumerate(variants)
    ]
    indexed, issues = index_verdicts(raw, keys)
    verdicts: list[dict] = []
    dropped: list[dict] = []

    for index, variant in enumerate(variants):
        key = keys[index]
        item = indexed.get(key)
        if item is None:
            dropped.append({"variant_key": key, "reason": "合规审查未返回该版本的结论"})
            continue
        missing = _missing_checks(item)
        if missing:
            dropped.append(
                {"variant_key": key, "reason": f"四类检查缺失：{', '.join(missing)}"}
            )
            continue
        verdict = build_verdict(
            {
                **{check: _raw_checks(item)[check] for check in CHECK_KEYS},
                "variant_id": key,
                "findings": item.get("findings"),
                "overridden_by": item.get("overridden_by"),
            }
        )
        # FR-519: a block without a citation is not reviewable. Treat it as an
        # incomplete review rather than showing the approver a bare "no".
        if verdict["severity"] == "block" and not verdict.get("findings"):
            dropped.append({"variant_key": key, "reason": "否决结论缺少依据（findings 为空）"})
            continue
        verdict["variant_key"] = key
        verdict["angle"] = _text(variant.get("angle"))
        verdict = apply_hard_ban(verdict, suspects.get(key, []))
        verdict["passed"] = verdict["severity"] != "block"
        # Attach the judgement in Chinese for the console and the CLI table.
        # Computed after the hard ban so an override cannot leave a stale "pass".
        verdict["headline"] = "；".join(
            f"{CHECK_LABELS[check]}: {severity_cn(verdict['checks'][check])}"
            for check in CHECK_KEYS
        )
        verdicts.append(verdict)
    return verdicts, dropped, issues


def severity_cn(value: str) -> str:
    return SEVERITY_CN.get(value, value)


def run_compliance(
    *,
    proposal: dict,
    product: SiteProduct,
    client: LLMClient,
    market: str = "US",
    trace: TraceRecorder | None = None,
    checker: ComplianceChecker | None = None,
) -> ComplianceResult:
    """One call per product, one repair if a variant has no verdict."""
    if not client.enabled:
        raise LLMError(f"model not configured: {client.settings.describe()}")

    checker = checker or ComplianceChecker()
    variants = list(proposal.get("variants") or [])
    if not variants:
        raise ComplianceError(str(proposal.get("product_id", "")), ["proposal has no variants"])

    suspects = lexicon_suspects(
        checker,
        variants,
        category=getattr(product, "category_slug", "") or "",
        market=market,
    )
    payload = build_payload(proposal, product, market=market, suspects=suspects)
    agent = ComplianceAgent(client, trace)

    verdicts: list[dict] = []
    dropped: list[dict] = []
    issues: list[str] = []
    calls = 0
    best: tuple[list[dict], list[dict], list[str]] | None = None

    for attempt in range(2):
        repair_issues = issues + [
            f"{item['variant_key']}: {item['reason']}" for item in dropped
        ]
        raw = agent.review(payload, repair_issues=repair_issues or None)
        calls += 1
        verdicts, dropped, issues = finalize_verdicts(raw, variants=variants, suspects=suspects)
        if best is None or (len(verdicts), -len(dropped)) > (len(best[0]), -len(best[1])):
            best = (verdicts, dropped, issues)
        if not dropped:
            break

    if best is not None:
        verdicts, dropped, issues = best

    result = ComplianceResult(
        product_id=str(proposal.get("product_id", "")),
        verdicts=verdicts,
        dropped=dropped,
        issues=issues,
        calls=calls,
        model=client.settings.model,
    )
    if not verdicts:
        raise ComplianceError(
            result.product_id,
            issues + [item["reason"] for item in dropped] or ["no verdict produced"],
        )
    return result
