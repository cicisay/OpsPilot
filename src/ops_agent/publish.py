"""发布编排：dry-run → 合规 → 审批令牌 → 加锁 → 幂等写入 → 失败补偿。

所有真实发布都要求一次性审批令牌；默认预览不写入。

合规否决是硬闸门：``review_results.json`` 里 severity=block 的版本，即使拿到
审批令牌也不写入。令牌只证明"人看过了"，不能推翻合规的否决；要发布必须先让
工坊重做并重新过审。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .approvals import ApprovalError
from .reviewer import ComplianceReviewer, ReviewDecision
from .site_platform import SiteAPIError, SitePlatform, SiteProduct

APPROVAL_THRESHOLD = 0.10
LOCK_STALE_SECONDS = 600


def load_verdicts(path: Path | str | None) -> dict[tuple[str, str], dict]:
    """Index review results by (product_id, variant_key).

    Old review files predate the four-check verdict; ``passed=false`` is mapped to
    block so a legacy file cannot accidentally unlock a variant.
    """
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    index: dict[tuple[str, str], dict] = {}
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("product_id") or ""), str(item.get("variant_key") or ""))
        if not key[1]:
            continue
        verdict = dict(item)
        if "severity" not in verdict:
            verdict["severity"] = "block" if item.get("passed") is False else "pass"
        index[key] = verdict
    return index


def verdict_for(
    verdicts: dict[tuple[str, str], dict], product_id: str, variant_key: str
) -> dict:
    return verdicts.get((str(product_id), str(variant_key)), {})


class PublishBusy(Exception):
    """另一发布进程正持有同一商品的锁。"""


@dataclass
class PublishPreview:
    product_id: str
    variant_id: str
    variant_key: str
    product_diff: dict
    variant_diff: dict
    price_change_pct: float
    requires_approval: bool
    review: ReviewDecision
    blocked: bool = False
    verdict: dict = field(default_factory=dict)


@dataclass
class PublishResult:
    ok: bool
    status: str
    message: str
    preview: PublishPreview | None = None
    product_version_id: str | None = None
    variant_version_id: str | None = None
    approved_by: str | None = None
    experiment_id: str = ""
    warnings: list[str] = field(default_factory=list)


def _safe_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", value) or "store"


class PublishOrchestrator:
    def __init__(
        self,
        site: SitePlatform,
        reviewer: ComplianceReviewer | None = None,
        approvals=None,
        store_slug: str = "",
        lock_dir: Path | str | None = None,
        verdict_path: Path | str | None = None,
        verdicts: dict[tuple[str, str], dict] | None = None,
        on_published=None,
    ) -> None:
        self.site = site
        self.reviewer = reviewer or ComplianceReviewer()
        self.approvals = approvals
        self.store_slug = _safe_key(store_slug)
        self.lock_dir = Path(lock_dir) if lock_dir else None
        self.verdicts = verdicts if verdicts is not None else load_verdicts(verdict_path)
        # Called only after the site accepted both writes: "publish registers the
        # experiment" has to happen on the success path, not in the caller's
        # bookkeeping, or a forgotten call loses the experiment forever.
        self.on_published = on_published

    def _lock_path(self, product_id: str) -> Path | None:
        if self.lock_dir is None:
            return None
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        return self.lock_dir / f"{_safe_key(product_id)}.lock"

    def _acquire_lock(self, product_id: str) -> Path | None:
        path = self._lock_path(product_id)
        if path is None:
            return None
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return path
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink(missing_ok=True)
                    return self._acquire_lock(product_id)
            except OSError:
                pass
            raise PublishBusy(f"publish lock busy: {product_id}") from None
        except OSError as exc:
            raise PublishBusy(f"publish lock error: {exc}") from exc

    @staticmethod
    def _release_lock(path: Path | None) -> None:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def preview(self, proposal: dict, product: SiteProduct, variant_index: int = 0) -> PublishPreview:
        variants_list = proposal.get("variants") or []
        if not 0 <= variant_index < len(variants_list):
            raise ValueError("variant_index out of range")
        variant = variants_list[variant_index]
        product_id = str(proposal["product_id"])

        detail = self.site.product(product_id)
        variants = detail.get("variants") or []
        if not variants:
            raise SiteAPIError(200, "no_variants", "product has no variants")

        target_id = proposal.get("target_variant_id")
        if target_id:
            entry = next((item for item in variants if item.get("variantId") == target_id), None)
            if entry is None:
                raise SiteAPIError(404, "variant_not_found", f"variant not found: {target_id}")
        else:
            entry = min(variants, key=lambda item: item["priceCents"])

        fields = {
            "titleEn": variant["title"],
            "bulletsEn": list(variant["bullets"]),
            "descEn": variant["description"],
        }
        product_diff = self.site.patch_product(
            product_id,
            fields,
            reason=f"agent preview: {variant.get('variant_key', '')}",
            dry_run=True,
        ).get("diff", {})
        variant_diff = self.site.patch_variant(
            entry["variantId"],
            price_cents=int(variant["price_cents"]),
            reason=f"agent preview: {variant.get('variant_key', '')}",
            dry_run=True,
        ).get("diff", {})

        old_price = int(entry["priceCents"])
        new_price = int(variant["price_cents"])
        price_change = abs(new_price - old_price) / old_price if old_price else 0.0
        market = str((proposal.get("task") or {}).get("market") or "US")
        review = self.reviewer.review(variant, product, market)
        variant_key = str(variant.get("variant_key", ""))
        verdict = verdict_for(self.verdicts, product_id, variant_key)
        blocked = str(verdict.get("severity") or "").lower() == "block"

        return PublishPreview(
            product_id=product_id,
            variant_id=str(entry["variantId"]),
            variant_key=variant_key,
            product_diff=product_diff,
            variant_diff=variant_diff,
            price_change_pct=round(price_change, 4),
            requires_approval=price_change > APPROVAL_THRESHOLD,
            review=review,
            blocked=blocked,
            verdict=verdict,
        )

    def publish(
        self,
        proposal: dict,
        product: SiteProduct,
        variant_index: int = 0,
        approval_token: str | None = None,
    ) -> PublishResult:
        preview = self.preview(proposal, product, variant_index)

        if preview.blocked:
            findings = preview.verdict.get("findings") or []
            reasons = "；".join(
                str(item.get("rule") or item.get("quote") or "").strip() for item in findings
            )[:240]
            return PublishResult(
                ok=False,
                status="blocked",
                message="合规否决（不可用审批令牌覆盖）：" + (reasons or "见审查结论"),
                preview=preview,
            )
        if not preview.review.passed:
            return PublishResult(
                ok=False,
                status="blocked",
                message="合规未通过：" + "；".join(preview.review.violations),
                preview=preview,
            )
        if not approval_token:
            return PublishResult(
                ok=False,
                status="approval_required",
                message="发布需要一次性审批令牌（approve issue 生成）",
                preview=preview,
            )
        if self.approvals is None:
            return PublishResult(
                ok=False, status="failed", message="审批存储未配置", preview=preview
            )

        try:
            lock_path = self._acquire_lock(preview.product_id)
        except PublishBusy as exc:
            return PublishResult(
                ok=False, status="busy", message=str(exc), preview=preview
            )

        try:
            try:
                record = self.approvals.consume(
                    approval_token, preview.product_id, preview.variant_key, "publish"
                )
            except ApprovalError as exc:
                return PublishResult(
                    ok=False,
                    status="invalid_approval",
                    message=f"审批令牌无效：{exc}",
                    preview=preview,
                )

            variant = proposal["variants"][variant_index]
            fields = {
                "titleEn": variant["title"],
                "bulletsEn": list(variant["bullets"]),
                "descEn": variant["description"],
            }
            reason = f"agent publish: {preview.variant_key}"
            prefix = f"pub-{self.store_slug}-{preview.product_id}-{preview.variant_key}"[:110]

            product_version: str | None = None
            variant_version: str | None = None
            try:
                product_result = self.site.patch_product(
                    preview.product_id,
                    fields,
                    reason=reason,
                    idempotency_key=f"{prefix}-p",
                )
                product_version = product_result.get("versionId")

                variant_result = self.site.patch_variant(
                    preview.variant_id,
                    price_cents=int(variant["price_cents"]),
                    reason=reason,
                    idempotency_key=f"{prefix}-v",
                )
                variant_version = variant_result.get("versionId")
            except SiteAPIError as exc:
                if product_version:
                    try:
                        self.site.rollback(
                            preview.product_id,
                            product_version,
                            reason="compensate after variant publish failure",
                        )
                    except SiteAPIError:
                        pass
                return PublishResult(
                    ok=False,
                    status="failed",
                    message=str(exc),
                    preview=preview,
                    product_version_id=product_version,
                    variant_version_id=variant_version,
                    approved_by=getattr(record, "issued_by", None),
                )

            experiment_id = ""
            warnings: list[str] = []
            if self.on_published is not None:
                try:
                    experiment_id = str(self.on_published(proposal, preview) or "")
                except Exception as exc:  # noqa: BLE001 - the copy is already live
                    # Rolling back a successful publish because the ledger failed
                    # would be worse than the missing line; surface it instead.
                    warnings.append(f"实验登记失败：{exc}")

            return PublishResult(
                ok=True,
                status="published",
                message="已发布",
                preview=preview,
                product_version_id=product_version,
                variant_version_id=variant_version,
                approved_by=getattr(record, "issued_by", None),
                experiment_id=experiment_id,
                warnings=warnings,
            )
        finally:
            self._release_lock(lock_path)
