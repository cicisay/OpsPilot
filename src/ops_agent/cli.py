"""Command line entry points for the project."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .approval_store import ApprovalStore
from .approvals import ApprovalService
from .agents.compliance_agent import ComplianceError, run_compliance
from .agents.director import new_run_id, run_director
from .agents.research import RESEARCH_BUDGET, ResearchError, run_research
from .agents.workshop_agent import WorkshopError
from .console.app import ConsoleApp
from .console.audit import AuditDB
from .console.auth import UserStore
from .console.db import ConsoleDB, SQLiteTenantStore, SQLiteUserStore, migrate_json
from .console.demo_site import SnapshotSite
from .console.server import ConsoleServer
from .console.tenants import Tenant, TenantStore
from .catalog import generate_catalog, load_catalog, save_catalog
from .compliance import ComplianceChecker, ensure_lexicon
from .config import DATA_DIR, PROJECT_ROOT, default_data_dir, default_lexicon_path, default_snapshot_path
from .config import llm_settings, load_env, provider_supports_tools
from .contracts import ContractError
from .experiment import (
    VERDICT_CN,
    ExperimentRegistrar,
    apply_judgement,
    baseline_for,
    build_record,
    clamp_window,
    experiment_id_for,
    is_due,
    judge,
    parse_day,
    summary as experiment_summary,
)
from .lessons import LessonError, derive_lessons
from .intake import classify_auto
from .intake.mapping import plan_intent
from .ingest import IngestError, build_drafts, read_supplier_csv, resolve_ingest_path, summarize as ingest_summary
from .memory import MemoryStore
from .models import jsonable
from .platform import MockPlatform
from .quota import QuotaPolicy
from .store_profile import load_store_profile
from .simulator import (
    apply_change,
    expected_ctr,
    expected_cvr,
    simulate_days,
    summarize,
)
from .reviewer import ComplianceReviewer
from .site_platform import SiteAPIError, SitePlatform, load_snapshot
from .supervisor import PROBLEM_LABELS, build_plan, diagnosis_to_dict, refine_plan
from .tools import ToolContext, build_registry
from .workshop import build_proposals, proposals_to_dict
from .llm import LLMClient, LLMError, TraceRecorder
from .publish import PublishOrchestrator, load_verdicts, verdict_for


def _width(text: str) -> int:
    """Approximate terminal width: CJK characters count as two columns."""

    return sum(2 if ord(char) > 127 else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    def render(row: list[str]) -> str:
        return "  ".join(_pad(str(cell), width) for cell, width in zip(row, widths)).rstrip()

    print(render(headers))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        print(render(row))


def _top_rows(skus, limit: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for rank, item in enumerate(summarize(skus)[:limit], start=1):
        sku = item["sku"]
        rows.append(
            [
                str(rank),
                sku.sku_id,
                item["label"],
                sku.market.value,
                f"{item['ctr'] * 100:.2f}%",
                f"{item['cvr'] * 100:.2f}%",
                f"{item['score']:.3f}",
            ]
        )
    return rows


def cmd_seed(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    skus = generate_catalog(args.count, args.seed)
    save_catalog(out_dir / "skus.json", skus)

    performance = []
    for sku in skus:
        performance.extend(simulate_days(sku, args.days, args.seed))

    csv_path = out_dir / "performance.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sku_id", "day", "impressions", "clicks", "orders", "returns"])
        for row in performance:
            writer.writerow(
                [row.sku_id, row.day.isoformat(), row.impressions, row.clicks, row.orders, row.returns]
            )

    print(f"生成 {len(skus)} 个 SKU，{len(performance)} 行表现数据")
    print(f"目录: {out_dir}")
    print()
    _table(
        ["#", "SKU", "类目", "市场", "CTR", "CVR", "机会分"],
        _top_rows(skus, 5),
        [3, 10, 12, 6, 8, 8, 8],
    )
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    data_dir = Path(args.data) if args.data else DATA_DIR
    catalog_path = data_dir / "skus.json"
    if not catalog_path.exists():
        print(f"找不到 {catalog_path}，请先运行: python run.py seed")
        return 1

    skus = load_catalog(catalog_path)
    print(f"{len(skus)} 个 SKU · 按机会分排序（越高越该先动）")
    print()
    _table(
        ["#", "SKU", "类目", "市场", "CTR", "CVR", "机会分"],
        _top_rows(skus, args.top),
        [3, 10, 12, 6, 8, 8, 8],
    )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    data_dir = Path(args.data) if args.data else DATA_DIR
    catalog_path = data_dir / "skus.json"
    if not catalog_path.exists():
        print(f"找不到 {catalog_path}，请先运行: python run.py seed")
        return 1

    skus = {sku.sku_id: sku for sku in load_catalog(catalog_path)}
    if args.sku not in skus:
        print(f"未知 SKU: {args.sku}")
        return 1

    before = skus[args.sku]
    try:
        after = apply_change(before, args.change, args.value)
    except ValueError as exc:
        print(f"改动无效: {exc}")
        return 1

    def line(label: str, sku) -> None:
        print(
            f"{label:<8} CTR {expected_ctr(sku) * 100:.2f}%  "
            f"CVR {expected_cvr(sku) * 100:.2f}%  "
            f"毛利 {sku.margin * 100:.1f}%  "
            f"价格 {sku.price:.2f}"
        )

    print(f"SKU {before.sku_id} · {before.title}")
    line("改前", before)
    line("改后", after)
    ctr_delta = (expected_ctr(after) - expected_ctr(before)) / expected_ctr(before) * 100
    cvr_delta = (expected_cvr(after) - expected_cvr(before)) / expected_cvr(before) * 100
    print(f"预期变化 CTR {ctr_delta:+.1f}%  CVR {cvr_delta:+.1f}%")
    return 0


def _build_context(data_dir: Path):
    platform = MockPlatform.from_data(data_dir)
    lexicon_path = default_lexicon_path()
    ensure_lexicon(lexicon_path)
    context = ToolContext(
        platform=platform,
        approvals=ApprovalService(),
        compliance=ComplianceChecker(lexicon_path),
    )
    return context, build_registry()


def cmd_tools_list(args: argparse.Namespace) -> int:
    registry = build_registry()
    rows = [
        [
            spec["name"],
            spec["side_effect"],
            "Y" if spec["requires_approval"] else "-",
            spec["description"],
        ]
        for spec in registry.specs()
    ]
    _table(["工具", "读写", "需审批", "说明"], rows, [22, 6, 6, 44])
    return 0


def _show(step: int, name: str, result, detail: str = "") -> None:
    status = "OK  " if result.ok else "FAIL"
    print(f"[{step}] {name:<20} {status}  {detail}")
    if not result.ok:
        print(f"       {result.code}: {result.error}")


def cmd_tools_demo(args: argparse.Namespace) -> int:
    data_dir = Path(args.data) if args.data else DATA_DIR
    if not (data_dir / "skus.json").exists():
        print(f"找不到 {data_dir / 'skus.json'}，请先运行: python run.py seed")
        return 1

    context, registry = _build_context(data_dir)
    sku = context.platform.skus.get(args.sku)
    if sku is None:
        print(f"未知 SKU: {args.sku}")
        return 1
    sku_id = sku.sku_id

    print(f"== 工具演示 · {sku_id} · {sku.title} ==")
    print()

    snapshot = registry.call("get_shop_snapshot", {"days": 7, "sku_id": sku_id}, context)
    detail = ""
    if snapshot.ok:
        item = snapshot.data["items"][0]
        detail = f"CTR {item['ctr'] * 100:.2f}% · CVR {item['cvr'] * 100:.2f}% · 曝光 {item['impressions']}"
    _show(1, "get_shop_snapshot", snapshot, detail)

    trends = registry.call(
        "get_trends",
        {"keyword": sku.category, "market": sku.market.value},
        context,
    )
    detail = ""
    if trends.ok:
        first, last = trends.data["points"][0], trends.data["points"][-1]
        detail = f"{first['month']} {first['index']} → {last['month']} {last['index']} · {trends.data['seasonality']}"
    _show(2, "get_trends", trends, detail)

    listing = registry.call(
        "generate_listing",
        {"sku_id": sku_id, "market": sku.market.value, "hypothesis": "scene_image", "variants": 2},
        context,
    )
    detail = ""
    if listing.ok:
        detail = f"{len(listing.data['variants'])} 版 · 预期 CTR {listing.data['expected_lift'] * 100:+.1f}%"
    _show(3, "generate_listing", listing, detail)
    if not listing.ok:
        return 1

    variant = dict(listing.data["variants"][0])
    text = " ".join([variant["title"], *variant["bullets"], variant["description"]])
    guard = registry.call(
        "compliance_check",
        {
            "text": text,
            "image_brief": variant["image_brief"],
            "category": sku.category,
            "market": sku.market.value,
        },
        context,
    )
    _show(4, "compliance_check", guard, f"passed={guard.data['passed']}" if guard.ok else "")

    new_price = round(sku.price * 0.88, 2)
    variant["price"] = new_price
    price = registry.call("price_calc", {"sku_id": sku_id, "price": new_price}, context)
    detail = ""
    if price.ok:
        detail = f"毛利 {price.data['margin'] * 100:.1f}% · 低于底线={price.data['below_floor']}"
    _show(5, "price_calc", price, detail)

    blocked = registry.call("publish_listing", {"sku_id": sku_id, "variant": variant}, context)
    _show(6, "publish_listing", blocked, "预期被拦截" if not blocked.ok else "（未拦截，异常）")

    token = context.approvals.issue(sku_id, "publish", "demo: 12% price cut")
    published = registry.call(
        "publish_listing",
        {"sku_id": sku_id, "variant": variant, "approval_token": token.token},
        context,
    )
    detail = ""
    if published.ok:
        detail = f"版本 v{published.data['version']} · {published.data['approved_by']}"
    _show(7, "publish_listing", published, detail)

    rolled = registry.call("rollback", {"sku_id": sku_id}, context)
    detail = ""
    if rolled.ok:
        detail = f"回到 v{rolled.data['restored_version']} · 价格 {rolled.data['price']:.2f}"
    _show(8, "rollback", rolled, detail)
    return 0


def cmd_intake_run(args: argparse.Namespace) -> int:
    """一句话委托 → 合约 dict 预览 / 上新草稿。本步只打印，绝不落库。"""

    text = (getattr(args, "text", "") or "").strip()
    if not getattr(args, "dry_run", False):
        # 写入路径留给 Step 8 的端到端；这里宁可报错，也不要让"没加 --dry-run"
        # 变成静默写入。诊断信息一律走 stderr：stdout 要留给 JSON / JSONL。
        print(
            "本步只支持 --dry-run：执行链路尚未接入（Step 8 才做端到端）。",
            file=sys.stderr,
        )
        return 2
    if not text:
        print(
            "缺少 --text，例如: python run.py intake run --text \"这20个新品上到美国站\" --dry-run",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "ingest", False):
        return _cmd_intake_ingest(args, text)

    result = classify_auto(text)
    ctx, ctx_notes = _intake_store_ctx(getattr(args, "snapshot", None))
    outcome = plan_intent(result, ctx)

    assumed: dict = {}
    for order in outcome.orders:
        assumed.update(order.get("slots", {}).get("assumed") or {})

    payload = {
        "source": "intent",
        "dry_run": True,
        "text": text,
        "intent": result.intent.value,
        "confidence": result.confidence,
        "engine": result.engine,
        "candidates": list(result.candidates),
        "missing": list(result.missing),
        "store": ctx["store_slug"],
        "context": {
            "snapshot": ctx["snapshot"],
            "products": len(ctx["products"]),
            "default_market": ctx["default_market"],
            "default_language": ctx["default_language"],
        },
        "assumed": assumed,
        "reason": outcome.reason,
        "notes": ctx_notes + list(outcome.notes),
        "orders": [jsonable(order) for order in outcome.orders],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_intake_ingest(args: argparse.Namespace, text: str) -> int:
    """供应商 CSV → Listing 草稿（JSONL 到 stdout）。

    stdout 只放草稿，别的都走 stderr：这个输出要能直接管道给下一步，
    混进一行中文摘要就解析不了了。
    """

    root = DATA_DIR / "ingest"
    origin = "data_dir"
    if getattr(args, "source", None):
        try:
            target, relative = resolve_ingest_path(args.source, root)
        except IngestError as exc:
            print(f"路径不接受：{exc.message}", file=sys.stderr)
            return 2
    else:
        candidates = sorted(root.glob("*.csv")) if root.is_dir() else []
        if candidates:
            target, relative = candidates[0], candidates[0].name
        else:
            # 仓库自带样例放在 tests/fixtures（data/ 不进版本库）。这里只读它，
            # 不往 data/ingest 复制文件——本步不做任何落盘。
            target = PROJECT_ROOT / "tests" / "fixtures" / "ingest" / "sample_supplier.csv"
            relative = target.name
            origin = "packaged_sample"

    try:
        parsed = read_supplier_csv(target)
    except IngestError as exc:
        print(f"供应商资料读不了：{exc.message}", file=sys.stderr)
        return 2

    ctx, ctx_notes = _intake_store_ctx(getattr(args, "snapshot", None))
    drafts = build_drafts(
        parsed.rows,
        market=ctx["default_market"],
        language=ctx["default_language"],
    )

    for draft in drafts:
        print(json.dumps(draft.to_dict(), ensure_ascii=False))

    summary = ingest_summary(drafts)
    counts = summary["status"]
    print(
        f"[ingest] {relative}（{origin}）· {summary['total']} 条草稿 · "
        f"ready {counts.get('ready', 0)} / needs_fill {counts.get('needs_fill', 0)} / "
        f"blocked {counts.get('blocked', 0)}",
        file=sys.stderr,
    )
    print(f"[ingest] 委托：{text}", file=sys.stderr)
    for note in [*parsed.notes, *ctx_notes]:
        print(f"[ingest] {note}", file=sys.stderr)
    if summary["flags"]:
        print(f"[ingest] 标记：{'、'.join(summary['flags'])}", file=sys.stderr)
    print("[ingest] dry-run：草稿不落库、不发布", file=sys.stderr)
    return 0


def _intake_store_ctx(path) -> tuple[dict, list[str]]:
    """读店铺快照，组装映射层要的上下文；读不到就给空候选，不编数据。"""

    notes: list[str] = []
    snapshot = Path(path) if path else default_snapshot_path()
    ctx = {
        "store_slug": "",
        "default_market": "US",
        "default_language": "en",
        "products": [],
        "snapshot": str(snapshot),
    }
    loaded = _load_snapshot_data(snapshot) if snapshot.exists() else None
    if loaded is None:
        notes.append(f"读不到快照 {snapshot}，商品候选为空（不编 SKU）")
        return ctx, notes

    store, products = loaded
    ctx["store_slug"] = store.slug
    country = str(store.country or "").strip().upper()
    if country in ("US", "DE", "UK"):
        ctx["default_market"] = country
    else:
        notes.append(f"快照国家是 {country or '空'}（卖家所在地，非目标市场），市场按 US 预演")
    ctx["products"] = [
        {
            "product_id": product.product_id,
            "title": product.title_en,
            "status": product.status,
            "price_cents": product.min_price_cents,
            "cost_cents": min(
                (variant.cost_cents for variant in product.variants if variant.cost_cents),
                default=None,
            ),
        }
        for product in products
    ]
    return ctx, notes


def _site_platform(args: argparse.Namespace):
    # .env 里的 SITE_AGENT_TOKEN / SITE_AGENT_BASE_URL 只有 load_env() 之后才进
    # os.environ。命令行每次新进程都从这里进，所以在这里补一次最省事；
    # load_env 不覆盖已存在的变量，shell 里显式 export 仍然优先。
    load_env()
    base = args.base_url or os.environ.get("SITE_AGENT_BASE_URL")
    try:
        return SitePlatform(base_url=base) if base else SitePlatform()
    except ValueError as exc:
        print(f"无法连接站点: {exc}")
        print('请先设置环境变量: $env:SITE_AGENT_TOKEN="..."')
        return None


def cmd_site_check(args: argparse.Namespace) -> int:
    site = _site_platform(args)
    if site is None:
        return 1
    try:
        health = site.health()
        store = site.store()
    except SiteAPIError as exc:
        print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
        return 1

    print(f"店铺: {store.name} ({store.slug}) · {store.currency} · {store.timezone}")
    print(f"API: {health.get('apiVersion')} · connector {health.get('connectorVersion')}")
    print(
        f"商品 {health.get('productCount')} / SKU {health.get('variantCount')} · "
        f"dbWritable={health.get('dbWritable')}"
    )
    print("capabilities: " + ", ".join(health.get("capabilities", [])))
    print(f"lastWriteAt: {health.get('lastWriteAt')}")
    return 0


def cmd_site_products(args: argparse.Namespace) -> int:
    site = _site_platform(args)
    if site is None:
        return 1
    try:
        products = site.products(limit=args.limit)
    except SiteAPIError as exc:
        print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
        return 1

    rows = []
    for product in products:
        metrics = product.metrics
        margin = product.min_margin
        rows.append(
            [
                product.title_en[:22],
                product.status,
                str(metrics.pv if metrics else 0),
                str(metrics.inquiries if metrics else 0),
                str(metrics.orders if metrics else 0),
                f"{margin * 100:.1f}%" if margin is not None else "—",
                str(product.stock_total),
            ]
        )
    _table(
        ["商品", "状态", "PV", "询盘", "订单", "毛利率", "库存"],
        rows,
        [24, 8, 8, 6, 6, 8, 8],
    )
    return 0


def cmd_site_sync(args: argparse.Namespace) -> int:
    site = _site_platform(args)
    if site is None:
        return 1
    try:
        store = site.store()
        health = site.health()
        products = site.all_products()
    except SiteAPIError as exc:
        print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
        return 1

    payload = {
        "store": asdict(store),
        "health": health,
        "products": [asdict(product) for product in products],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    out = Path(args.out) if args.out else default_snapshot_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"同步 {len(products)} 个商品 -> {out}")
    return 0


def cmd_site_dry_run(args: argparse.Namespace) -> int:
    site = _site_platform(args)
    if site is None:
        return 1
    try:
        result = site.patch_variant(
            args.variant_id,
            price_cents=args.price_cents,
            stock=args.stock,
            dry_run=True,
        )
    except (SiteAPIError, ValueError) as exc:
        print(f"预演失败: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _load_snapshot_data(path):
    snapshot = Path(path)
    if not snapshot.exists():
        print(f"找不到快照: {snapshot}")
        print("请先运行: python run.py site sync")
        return None
    try:
        return load_snapshot(snapshot)
    except (OSError, ValueError, KeyError) as exc:
        print(f"快照读取失败: {exc}")
        return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _limit_value(raw, *, auto: int) -> int:
    """``--limit`` accepts a number or ``auto``; rules need a number, models do not."""
    text = str(raw if raw is not None else "").strip().lower()
    if text in ("", "auto", "0"):
        return auto
    try:
        value = int(text)
    except ValueError:
        return auto
    return value if value > 0 else auto


def _limit_override(raw) -> int:
    """Zero means "let the model decide"; a number is a human ceiling."""
    text = str(raw if raw is not None else "").strip().lower()
    if text in ("", "auto", "0"):
        return 0
    try:
        value = int(text)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _fetch_reviews(site, products, limit: int = 20) -> dict[str, list[dict]]:
    """Pull review text only where the metrics say there is something to read.

    This is payload shaping, not judgement: the director still decides which of
    these products matter. Fetching all 50 details would triple the run time for
    products that have no negative review to learn from.
    """
    targets = [item for item in products if item.metrics and item.metrics.negative_reviews > 0]
    targets.sort(key=lambda item: item.metrics.negative_reviews, reverse=True)
    reviews: dict[str, list[dict]] = {}
    for product in targets[:limit]:
        try:
            detail = site.product(product.product_id)
        except SiteAPIError:
            continue
        items = detail.get("reviews")
        if isinstance(items, list):
            reviews[product.product_id] = [item for item in items if isinstance(item, dict)]
    return reviews


def _resolve_runtime(requested) -> str:
    """Explicit flag wins, then ``OPS_RUNTIME``, then the old one-shot path."""
    text = str(requested if requested is not None else "").strip().lower()
    if text in ("oneshot", "agent"):
        return text
    # .env is read here too: OPS_RUNTIME is usually set there, not exported,
    # and this is called before any code path that would have loaded it.
    load_env()
    env = (os.environ.get("OPS_RUNTIME") or "").strip().lower()
    return env if env in ("oneshot", "agent") else "oneshot"


def _print_agent_tasks(payload: dict) -> None:
    rows = []
    for index, task in enumerate(payload.get("selected") or [], start=1):
        band = task.get("price_band_cents") or [0, 0]
        rows.append(
            [
                str(index),
                _clip(str(task.get("title") or ""), 22),
                _clip(str(task.get("problem_label") or ""), 10),
                f"{float(task.get('score') or 0):.2f}",
                f"${int(band[0]) / 100:.2f}–${int(band[1]) / 100:.2f}",
                f"{task.get('variant_min')}–{task.get('variant_max')}",
                f"{len(task.get('evidence_refs') or [])} 条",
            ]
        )
    _table(["#", "商品", "主问题", "优先", "价格带", "版数", "证据"], rows, [3, 22, 12, 5, 15, 6, 6])


def _freshness(args) -> dict:
    """这批数字是哪天的？任务单必须能自己回答。

    在线模式读站点实时接口，新鲜度就是今天；离线/快照模式读 ``site sync``
    拍下的文件，取它的 ``fetched_at`` 算距今几天。两头都拿不到时给 unknown，
    不许假装新鲜——旧照片最容易被当成刚体检过。
    """
    today = date.today()
    if not getattr(args, "offline", False):
        return {"data_as_of": today.isoformat(), "snapshot_age_days": 0, "data_source": "live"}
    path = Path(getattr(args, "snapshot", None) or default_snapshot_path())
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"data_as_of": "", "snapshot_age_days": None, "data_source": "snapshot"}
    raw = str(raw_payload.get("fetched_at") or "")
    day = None
    if raw:
        try:
            day = datetime.fromisoformat(raw).date()
        except ValueError:
            day = None
    if day is None:
        return {"data_as_of": "", "snapshot_age_days": None, "data_source": "snapshot"}
    return {
        "data_as_of": day.isoformat(),
        "snapshot_age_days": max(0, (today - day).days),
        "data_source": "snapshot",
    }


def _apply_freshness(payload: dict, args) -> dict:
    """写进任务单，并在终端把人话打出来，过期就提醒先同步。"""
    payload.update(_freshness(args))
    as_of = payload.get("data_as_of") or ""
    age = payload.get("snapshot_age_days")
    if not as_of:
        print("数据: 日期未知（快照缺少 fetched_at，建议重新 site sync）")
        return payload
    if payload.get("data_source") == "live":
        print(f"数据: 站点实时读取（{as_of}）")
        return payload
    label = "今天" if age == 0 else f"{age} 天前"
    print(f"数据: 快照截至 {as_of}（{label}）")
    if isinstance(age, int) and age > 10:
        print(f"[WARN] 快照已过期 {age} 天，先跑 python run.py site sync 再出体检更可靠")
    return payload


def _store_profile(store, args):
    """Store settings live next to the artefacts this run is about to write.

    ``--out`` already points at the store's data directory (the console passes
    the tenant directory explicitly), so it is the most reliable hint available
    and avoids a second path convention.
    """
    out = getattr(args, "out", None)
    data_dir = Path(out).parent if out else None
    profile = load_store_profile(store, data_dir=data_dir)
    for note in profile.notes:
        print(f"[WARN] 店铺档案: {note}")
    return profile


def _run_agent_director(
    args,
    *,
    store,
    products,
    client,
    memory,
    profile,
    reviews,
    limit_override: int,
    run_id: str,
    trace,
) -> int:
    """--runtime agent: the model decides which products to open, not just what to say."""
    settings = client.settings
    policy = profile.quota
    if not provider_supports_tools(settings.provider):
        print(f"[ERROR] provider={settings.provider} 不支持原生 function calling。")
        print("        --runtime agent 需要 tools 能力；请改回 --runtime oneshot，")
        print("        或换用 deepseek / qwen / glm / moonshot / minimax / openai。")
        print("        自建 ollama 需显式设置 LLM_TOOLS=1。")
        return 1

    print("红线: 只读工具 · 每条结论必须引用本次观察编号 · 写入工具不对模型开放")
    print(f"模型: {settings.describe()}")
    print(
        f"运行模式: agent · 预算 {RESEARCH_BUDGET.max_steps} 步 / "
        f"{RESEARCH_BUDGET.max_tool_calls} 次工具调用 / "
        f"{RESEARCH_BUDGET.max_total_tokens} token / {int(RESEARCH_BUDGET.max_wall_seconds)}s"
    )
    print(f"配额: {profile.describe()}" + (" · 记忆已启用" if memory else " · 本次不写记忆"))
    print()

    try:
        payload = run_research(
            store=store,
            products=products,
            client=client,
            memory=memory,
            policy=policy,
            run_id=run_id,
            trace=trace,
            reviews=reviews,
            days=getattr(args, "days", 30),
            limit_override=limit_override,
            margin_floor=profile.margin_floor,
            window_max_days=profile.window_max_days,
            band_floor_ratio=profile.band_floor_ratio,
            band_ceil_ratio=profile.band_ceil_ratio,
        )
    except ResearchError as exc:
        print("[ABORT] agent 未产出可执行任务单（不回落规则基线）：")
        for item in exc.errors[:12]:
            print(f"  - {item}")
        summary = trace.summary()
        print(f"调用 {summary['calls']} 次 · trace {summary['path']}")
        return 1
    except LLMError as exc:
        print(f"[ABORT] 模型调用失败：{exc}")
        summary = trace.summary()
        print(f"调用 {summary['calls']} 次 · trace {summary['path']}")
        return 1

    health = payload.get("store_health") or {}
    score = health.get("score")
    print(f"店铺: {store.name} ({store.slug}) · 扫描 {payload.get('scanned')} 个在售商品")
    print(f"体检: {score if score is not None else '—'}/100 · {health.get('summary') or '—'}")
    if health.get("weak_categories"):
        print(f"异常类目: {', '.join(health['weak_categories'])}")
    print(f"本周: 改 {len(payload.get('selected') or [])} 个（agent 决定，证据可回溯到 obs 编号）")
    print()
    _print_agent_tasks(payload)

    _apply_freshness(payload, args)
    out = Path(args.out) if getattr(args, "out", None) else default_data_dir() / "task_orders.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"TaskOrder 已写入 {out}")
    for note in payload.get("notes") or []:
        print(f"- {note}")
    violations = payload.get("violations") or []
    if violations:
        print(f"[WARN] {len(violations)} 条校验提示，前 3 条：")
        for item in violations[:3]:
            print(f"  - {item}")
    summary = trace.summary()
    print(
        f"agent 调用 {summary['calls']} 次 · token {summary['tokens']} · trace {summary['path']}"
    )
    if memory is not None:
        print(f"记忆已更新: {memory.root}")
    return 0


def cmd_director_run(args: argparse.Namespace) -> int:
    """The weekly decision, made by the director model and bounded by code.

    Two runtimes, one output contract: ``oneshot`` makes a single judgement from
    a prepared digest, ``agent`` lets the model call read-only tools first.
    """
    settings = llm_settings()
    if not settings.enabled:
        print(f"[ERROR] 模型未配置（{settings.describe()}）。")
        print("        主管不做规则兜底，请在 .env 设置 LLM_PROVIDER / LLM_API_KEY 后重试。")
        return 1

    client = LLMClient(settings=settings)
    # One id for the business run and for the trace file, so a task order can
    # be traced back to the exact calls that produced it.
    run_id = new_run_id()
    trace = TraceRecorder(run_id=run_id)
    reviews: dict[str, list[dict]] = {}
    # Stays None in --offline mode, where judging falls back to the local file.
    site = None

    if getattr(args, "offline", False):
        loaded = _load_snapshot_data(args.snapshot or default_snapshot_path())
        if loaded is None:
            return 1
        store, products = loaded
    else:
        site = _site_platform(args)
        if site is None:
            return 1
        try:
            store = site.store()
            products = site.all_products()
        except SiteAPIError as exc:
            print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
            return 1
        reviews = _fetch_reviews(site, products)
        print(f"读取评价原文：{len(reviews)} 个商品（仅有差评记录的）")

    profile = _store_profile(store, args)
    policy = profile.quota
    memory = None if getattr(args, "no_memory", False) else MemoryStore()
    limit_override = _limit_override(getattr(args, "limit", "auto"))

    if memory is not None:
        closed = _judge_due_experiments(memory, site=site)
        if closed:
            supported = sum(1 for item in closed if item.get("verdict") == "supported")
            rejected = sum(1 for item in closed if item.get("verdict") == "rejected")
            print(
                f"到期实验: 判定 {len(closed)} 条（成立 {supported} · 不成立 {rejected}）"
                " · 经验库可用 experiment judge 回灌"
            )

    runtime = _resolve_runtime(getattr(args, "runtime", None))
    if runtime != "agent":
        # The agent branch prints its own header: its red line is about read-only
        # tools, not about which fields may be edited.
        print("红线: 只改 titleEn/bulletsEn/descEn · 价格只能落在代码算出的价位点 · 写入工具不对模型开放")
        print(f"模型: {settings.describe()}")
        print(f"配额: {profile.describe()}" + (" · 记忆已启用" if memory else " · 本次不写记忆"))
        print()

    if runtime == "agent":
        return _run_agent_director(
            args,
            store=store,
            products=products,
            client=client,
            memory=memory,
            profile=profile,
            reviews=reviews,
            limit_override=limit_override,
            run_id=run_id,
            trace=trace,
        )

    try:
        result = run_director(
            store=store,
            products=products,
            client=client,
            reviews=reviews,
            trace=trace,
            memory=memory,
            policy=policy,
            days=getattr(args, "days", 30),
            run_id=run_id,
            limit_override=limit_override,
            margin_floor=profile.margin_floor,
            window_max_days=profile.window_max_days,
            band_floor_ratio=profile.band_floor_ratio,
            band_ceil_ratio=profile.band_ceil_ratio,
        )
    except ContractError as exc:
        print("[ABORT] 主管输出未通过校验，整批中止（不产出规则兜底）：")
        for item in exc.errors[:12]:
            print(f"  - {item}")
        summary = trace.summary()
        print(f"调用 {summary['calls']} 次 · trace {summary['path']}")
        return 1
    except LLMError as exc:
        print(f"[ABORT] 模型调用失败：{exc}")
        summary = trace.summary()
        print(f"调用 {summary['calls']} 次 · trace {summary['path']}")
        return 1

    health = result.store_health
    print(f"店铺: {store.name} ({store.slug}) · 扫描 {result.scanned} 个在售商品")
    print(f"体检: {health.get('score')}/100 · {health.get('summary') or '—'}")
    if health.get("weak_categories"):
        print(f"异常类目: {', '.join(health['weak_categories'])}")
    print(
        f"本周: 改 {len(result.selected)} 个（模型请求 {result.requested_count}，"
        f"常态 {result.quota.get('low')} · 上限 {result.quota.get('high')}）"
    )
    if result.underfill_reason:
        print(f"减量说明: {result.underfill_reason}")
    print()

    rows = []
    for index, task in enumerate(result.selected, start=1):
        low, high = task["price_band_cents"]
        rows.append(
            [
                str(index),
                _clip(task["title"], 22),
                _clip(task["problem_label"], 10),
                f"{task['score']:.2f}",
                f"${low / 100:.2f}–{high / 100:.2f}",
                f"{task['variant_min']}–{task['variant_max']}",
                _clip(task["evidence"][0] if task["evidence"] else "", 26),
            ]
        )
    _table(["#", "商品", "主问题", "优先", "价格带", "版数", "首要证据"], rows, [3, 22, 12, 5, 13, 6, 26])

    payload = result.as_payload()
    _apply_freshness(payload, args)
    out = Path(args.out) if getattr(args, "out", None) else default_data_dir() / "task_orders.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"TaskOrder 已写入 {out}")
    for note in result.notes:
        print(f"- {note}")
    if result.violations:
        print(f"[WARN] {len(result.violations)} 条校验提示，前 3 条：")
        for item in result.violations[:3]:
            print(f"  - {item}")
    summary = trace.summary()
    print(f"主管调用 {summary['calls']} 次 · token {summary['tokens']} · trace {summary['path']}")
    if memory is not None:
        print(f"记忆已更新: {memory.root}")
    return 0


def cmd_supervisor_run(args: argparse.Namespace) -> int:
    # 默认 rule 是历史行为；--planner auto 会按模型是否配置分流，一键脚本用它
    # 才能"有 key 出体检分，没 key 也能跑完并说明本次没有体检分"。
    requested = str(getattr(args, "planner", "rule") or "rule").lower()
    settings = llm_settings()
    planner = _resolve_planner(requested, settings)
    runtime = _resolve_runtime(getattr(args, "runtime", None))
    if planner == "llm" or runtime == "agent":
        # The model path is the director. One decision, no rule shortlist.
        code = cmd_director_run(args)
        # 只有 auto 才允许降级：显式点名 llm 的人，失败就该看见失败。
        # 现实里最常见的失败不是"没配 key"，而是 key 欠费/额度用尽（429），
        # 那种情况下一键脚本必须还能跑完，否则运营台整天停在旧数据上。
        if code == 0 or requested != "auto" or runtime == "agent":
            return code
        print("[WARN] 主管没跑通（模型报错见上），降级到规则引擎：本次不产出体检分。")
    elif requested == "auto":
        print(f"[WARN] 模型未配置（{settings.describe()}），本次走规则引擎：不产出体检分。")
        print("       运营台首页的体检卡片会留空；配好 LLM_API_KEY 后重跑即出分。")

    args.limit = _limit_value(getattr(args, "limit", 5), auto=5)
    site = None
    if args.offline:
        loaded = _load_snapshot_data(args.snapshot or default_snapshot_path())
        if loaded is None:
            return 1
        store, products = loaded
    else:
        site = _site_platform(args)
        if site is None:
            return 1
        try:
            store = site.store()
            products = site.all_products()
        except SiteAPIError as exc:
            print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
            return 1

    plan = build_plan(products, store.slug, limit=args.limit, days=args.days)

    print(f"店铺: {store.name} ({store.slug})")
    print(f"扫描 {plan.scanned} 个在售商品 · 命中 {plan.matched} 个 · 取前 {len(plan.selected)} 个 · planner=rule (legacy)")
    print()

    rows = []
    for index, diagnosis in enumerate(plan.selected, start=1):
        rows.append(
            [
                str(index),
                _clip(diagnosis.product.title_en, 24),
                PROBLEM_LABELS[diagnosis.problem],
                f"{diagnosis.score:.2f}",
                _clip(diagnosis.action, 12),
                _clip("；".join(diagnosis.evidence), 20),
            ]
        )
    _table(["#", "商品", "问题", "分数", "建议", "证据"], rows, [3, 24, 8, 6, 24, 40])

    payload = {
        "store": store.slug,
        "generated_at": plan.generated_at,
        "scanned": plan.scanned,
        "selected": [diagnosis_to_dict(diagnosis) for diagnosis in plan.selected],
        "notes": plan.notes,
    }
    _apply_freshness(payload, args)

    out = Path(args.out) if args.out else default_data_dir() / "task_orders.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"TaskOrder 已写入 {out}")
    for note in plan.notes:
        print(f"- {note}")
    print("提示：规则基线仅用于对照；模型路径请用: python run.py director run")
    return 0


def _resolve_planner(requested: str, settings) -> str:
    """auto: use the model when it is configured, the rule baseline otherwise."""
    requested = (requested or "auto").lower()
    if requested == "auto":
        return "llm" if settings.enabled else "rule"
    return requested


def cmd_workshop_run(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks) if args.tasks else default_data_dir() / "task_orders.json"
    if not tasks_path.exists():
        print(f"找不到 {tasks_path}，请先运行: python run.py director run")
        return 1
    tasks = json.loads(tasks_path.read_text(encoding="utf-8")).get("selected", [])
    if not tasks:
        print("任务单为空，请先跑 director run")
        return 1
    # The task file already reflects the week's decision; by default the
    # workshop builds every one of them instead of an arbitrary first three.
    args.limit = min(_limit_value(getattr(args, "limit", "auto"), auto=len(tasks)), len(tasks))

    settings = llm_settings()
    planner = _resolve_planner(getattr(args, "planner", "auto"), settings)
    if planner == "llm" and not settings.enabled:
        print(f"[WARN] 模型未配置（{settings.describe()}），本次使用规则基线。")
        planner = "rule"
    client = LLMClient(settings=settings)
    trace = TraceRecorder() if planner == "llm" else None

    if planner == "llm":
        print("红线: 只改 titleEn/bulletsEn/descEn · 价格仅从价位点选 · 数字必须可溯源到商品事实")
        print("     版数由模型在任务单区间内决定；失败按校验清单重写，不回落规则模板")

    reviews: dict[str, list[dict]] = {}

    if args.offline:
        snapshot = Path(args.snapshot) if args.snapshot else default_snapshot_path()
        loaded = _load_snapshot_data(snapshot)
        if loaded is None:
            return 1
        store, products = loaded
        details: dict[str, dict] = {}
    else:
        site = _site_platform(args)
        if site is None:
            return 1
        try:
            store = site.store()
            products = site.all_products()
            details = {}
            for task in tasks[: args.limit]:
                product_id = str(task.get("product_id"))
                try:
                    details[product_id] = site.product(product_id)
                except SiteAPIError:
                    continue
                items = details[product_id].get("reviews")
                if isinstance(items, list) and items:
                    reviews[product_id] = [item for item in items if isinstance(item, dict)]
        except SiteAPIError as exc:
            print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
            return 1

    products_by_id = {product.product_id: product for product in products}
    failures: list[dict] = []
    proposals = build_proposals(
        tasks,
        products_by_id,
        details,
        limit=args.limit,
        market=args.market,
        planner=planner,
        client=client if planner == "llm" else None,
        trace=trace,
        reviews=reviews,
        failures=failures,
    )

    print(f"店铺: {store.name} ({store.slug})")
    print(f"任务单 {len(tasks)} 个 · 生成 {len(proposals)} 个方案包 · planner={planner}")
    if planner == "llm":
        print(f"模型: {settings.describe()}")
    print()

    rows = []
    for index, proposal in enumerate(proposals, start=1):
        total = len(proposal.variants)
        passed = sum(1 for variant in proposal.variants if variant.compliance_passed)
        prices = [variant.price_cents for variant in proposal.variants]
        price_range = f"${min(prices) / 100:.2f}–{max(prices) / 100:.2f}" if prices else "—"
        rows.append(
            [
                str(index),
                _clip(proposal.title, 24),
                PROBLEM_LABELS.get(proposal.problem, proposal.problem),
                str(total),
                f"{passed}/{total}",
                price_range,
                str(proposal.critique.get("attempts", "—")) if proposal.critique else "—",
            ]
        )
    _table(
        ["#", "商品", "问题", "方案数", "词表", "价格带", "尝试"],
        rows,
        [3, 24, 8, 6, 6, 14, 5],
    )

    payload = proposals_to_dict(store.slug, proposals)
    payload["failures"] = failures
    out = Path(args.out) if args.out else default_data_dir() / "workshop_proposals.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"方案包已写入 {out}")

    if failures:
        print(f"[WARN] {len(failures)} 个任务未能产出合格方案（未回落模板）：")
        for item in failures[:4]:
            print(f"  - {str(item.get('product_id', ''))[:8]}: {_clip(item.get('reason', ''), 80)}")
    if trace is not None:
        summary = trace.summary()
        print(
            f"工坊调用 {summary['calls']} 次 · token {summary['tokens']} · trace {summary['path']}"
        )
    return 0 if proposals else 1


def cmd_agent_run(args: argparse.Namespace) -> int:
    """One command: director -> workshop -> compliance, in that order."""
    tasks_out = (
        Path(args.tasks_out) if getattr(args, "tasks_out", None) else default_data_dir() / "task_orders.json"
    )
    proposals_out = (
        Path(args.proposals_out)
        if getattr(args, "proposals_out", None)
        else default_data_dir() / "workshop_proposals.json"
    )
    review_out = (
        Path(args.review_out) if getattr(args, "review_out", None) else default_data_dir() / "review_results.json"
    )

    supervisor_args = argparse.Namespace(
        offline=args.offline,
        snapshot=args.snapshot,
        limit=args.limit,
        days=args.days,
        out=str(tasks_out),
        base_url=args.base_url,
        planner=args.planner,
        runtime=getattr(args, "runtime", None),
    )
    code = cmd_supervisor_run(supervisor_args)
    if code != 0:
        return code

    workshop_args = argparse.Namespace(
        offline=args.offline,
        snapshot=args.snapshot,
        tasks=str(tasks_out),
        limit=args.limit,
        market=args.market,
        out=str(proposals_out),
        planner=args.planner,
        base_url=args.base_url,
    )
    code = cmd_workshop_run(workshop_args)
    if code != 0:
        return code

    review_args = argparse.Namespace(
        offline=args.offline,
        snapshot=args.snapshot,
        proposals=str(proposals_out),
        limit="auto",
        market=args.market,
        out=str(review_out),
        planner=args.planner,
        base_url=args.base_url,
    )
    return cmd_review_run(review_args)


def cmd_approve_issue(args: argparse.Namespace) -> int:
    if not os.environ.get("OPS_APPROVER_TOKEN"):
        print("请先设置审批人密钥: $env:OPS_APPROVER_TOKEN=\"...\"")
        return 1
    # A token proves a human looked at the copy; it cannot overrule compliance.
    review_path = (
        Path(args.review) if getattr(args, "review", None) else default_data_dir() / "review_results.json"
    )
    verdict = verdict_for(load_verdicts(review_path), args.product_id, args.variant_key)
    if str(verdict.get("severity") or "").lower() == "block":
        rules = "；".join(
            str(item.get("rule") or "") for item in verdict.get("findings") or []
        )[:200]
        print(f"[拒绝签发] 该版本已被合规否决：{rules or '见审查结论'}")
        print("请让工坊重做并重新过审，审批令牌不能覆盖否决。")
        return 1
    try:
        record = ApprovalStore(DATA_DIR / "approvals.json").issue(
            args.product_id,
            args.variant_key,
            args.operator,
            ttl_seconds=args.ttl,
        )
    except (OSError, ValueError) as exc:
        print(f"签发失败: {exc}")
        return 1
    print(f"审批令牌: {record.token}")
    print(
        f"对象: {record.product_id} / {record.variant_key} · "
        f"签发人 {record.issued_by} · 到期 {record.expires_at}"
    )
    print("只把令牌交给执行发布的人；令牌一次有效。")
    return 0


def cmd_approve_list(args: argparse.Namespace) -> int:
    records = ApprovalStore(DATA_DIR / "approvals.json").list_active()
    if not records:
        print("没有未使用的审批令牌")
        return 0
    rows = [
        [
            record.token[:10] + "…",
            record.product_id,
            record.variant_key,
            record.issued_by,
            record.expires_at,
        ]
        for record in records
    ]
    _table(["令牌", "商品", "变体", "签发人", "到期"], rows, [14, 26, 14, 10, 22])
    return 0


def _load_proposals(args: argparse.Namespace):
    path = Path(args.proposals) if args.proposals else default_data_dir() / "workshop_proposals.json"
    if not path.exists():
        print(f"找不到 {path}，请先运行: python run.py workshop run")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("proposals", [])
    except (OSError, ValueError) as exc:
        print(f"方案包读取失败: {exc}")
        return None


def _rule_review(
    proposal: dict, product, market: str, planner: str
) -> list[dict]:
    """Legacy lexical review, kept as the offline baseline."""
    reviewer = ComplianceReviewer()
    entries: list[dict] = []
    for variant in proposal.get("variants") or []:
        decision = reviewer.review(variant, product, market=market)
        entries.append(
            {
                "product_id": proposal["product_id"],
                "variant_key": variant.get("variant_key", ""),
                "angle": variant.get("angle", ""),
                "passed": decision.passed,
                "severity": "pass" if decision.passed else "block",
                "checks": {},
                "findings": [
                    {"category": "rule", "quote": "", "rule": item, "suggestion": ""}
                    for item in decision.violations
                ],
                "violations": list(decision.violations),
                "suggestions": list(decision.suggestions),
                "overridden_by": "",
                "rule_version": decision.rule_version,
                "market": market,
                "planner": planner,
                "model": "",
            }
        )
    return entries


def _failed_review_entries(proposal: dict, reason: str, plan: str) -> list[dict]:
    """No verdict means no approval: mark every variant block with the reason."""
    return [
        {
            "product_id": proposal["product_id"],
            "variant_key": variant.get("variant_key", ""),
            "angle": variant.get("angle", ""),
            "passed": False,
            "severity": "block",
            "checks": {},
            "findings": [
                {
                    "category": "review_incomplete",
                    "quote": "",
                    "rule": f"合规审查未完成：{reason}",
                    "suggestion": "重跑审查或让工坊重做",
                }
            ],
            "violations": [reason],
            "suggestions": ["重跑 review run"],
            "overridden_by": "",
            "market": "",
            "planner": plan,
            "model": "",
            "dropped": True,
        }
        for variant in proposal.get("variants") or []
    ]


def cmd_review_run(args: argparse.Namespace) -> int:
    proposals = _load_proposals(args)
    if proposals is None:
        return 1
    if args.offline:
        loaded = _load_snapshot_data(args.snapshot or default_snapshot_path())
        if loaded is None:
            return 1
        _, products = loaded
    else:
        site = _site_platform(args)
        if site is None:
            return 1
        try:
            products = site.all_products()
        except SiteAPIError as exc:
            print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
            return 1

    limit = _limit_value(getattr(args, "limit", "auto"), auto=len(proposals))
    backfilled = ensure_lexicon(default_lexicon_path())
    if backfilled:
        # Silence here is how 33 of 50 SKUs ran without any market rules at all.
        print(f"[WARN] 词库缺少 {'、'.join(backfilled)}，已按基线补齐，请确认这些规则仍需生效。")
    settings = llm_settings()
    planner = _resolve_planner(getattr(args, "planner", "auto"), settings)
    if planner == "llm" and not settings.enabled:
        print(f"[WARN] 模型未配置（{settings.describe()}），本次使用词表基线。")
        planner = "rule"
    client = LLMClient(settings=settings)
    trace = TraceRecorder() if planner == "llm" else None

    products_by_id = {product.product_id: product for product in products}
    rows = []
    results = []
    dropped: list[dict] = []

    for proposal in proposals[:limit]:
        product = products_by_id.get(proposal["product_id"])
        if product is None:
            continue
        if planner == "llm":
            try:
                outcome = run_compliance(
                    proposal=proposal,
                    product=product,
                    client=client,
                    market=args.market,
                    trace=trace,
                )
                entries = [
                    {
                        **verdict,
                        "product_id": proposal["product_id"],
                        "violations": [
                            f"{item.get('category')}:{item.get('rule')}"
                            for item in verdict.get("findings") or []
                        ],
                        "suggestions": [
                            item.get("suggestion") for item in verdict.get("findings") or []
                        ],
                        "market": args.market,
                        "planner": "llm",
                        "model": outcome.model,
                    }
                    for verdict in outcome.verdicts
                ]
                for item in outcome.dropped:
                    dropped.append({"product_id": proposal["product_id"], **item})
                    entries.append(
                        {
                            "product_id": proposal["product_id"],
                            "variant_key": item["variant_key"],
                            "angle": "",
                            "passed": False,
                            "severity": "block",
                            "checks": {},
                            "findings": [
                                {
                                    "category": "review_incomplete",
                                    "quote": "",
                                    "rule": item["reason"],
                                    "suggestion": "重跑审查或让工坊重做",
                                }
                            ],
                            "violations": [item["reason"]],
                            "suggestions": ["重跑审查"],
                            "overridden_by": "",
                            "market": args.market,
                            "planner": "llm",
                            "model": outcome.model,
                            "dropped": True,
                        }
                    )
            except (ComplianceError, LLMError) as exc:
                entries = _failed_review_entries(proposal, str(exc), "llm")
        else:
            entries = _rule_review(proposal, product, args.market, "rule")

        for entry in entries:
            results.append(entry)
            rows.append(
                [
                    _clip(proposal["title"], 20),
                    _clip(entry.get("angle", ""), 8),
                    {"block": "否决", "warn": "提示", "pass": "通过"}.get(
                        entry.get("severity", ""), "?"
                    ),
                    _clip(" ".join(entry.get("violations") or []) or "—", 30),
                ]
            )

    _table(["商品", "角度", "结论", "依据"], rows, [22, 10, 6, 34])
    counts = {
        "block": sum(1 for item in results if item.get("severity") == "block"),
        "warn": sum(1 for item in results if item.get("severity") == "warn"),
        "pass": sum(1 for item in results if item.get("severity") == "pass"),
        "dropped": len(dropped),
    }
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "planner": planner,
        "model": settings.model if planner == "llm" else "",
        "market": args.market,
        "summary": counts,
        "dropped": dropped,
        "results": results,
    }
    out = Path(args.out) if args.out else default_data_dir() / "review_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(
        f"审查结果已写入 {out} · 否决 {counts['block']} · 提示 {counts['warn']} · "
        f"通过 {counts['pass']} · 无结论 {counts['dropped']}"
    )
    if counts["block"]:
        print("否决版本不会进入审批队列；审批令牌不能覆盖合规否决。")
    if trace is not None:
        summary = trace.summary()
        print(
            f"合规调用 {summary['calls']} 次 · token {summary['tokens']} · trace {summary['path']}"
        )
    return 0


def _baseline_provider(args: argparse.Namespace, products_by_id: dict):
    """Where the pre-publish baseline comes from, offline vs live.

    Offline runs read the local daily series (``performance.csv``) because the
    judge needs per-day rows; live runs only have the product's 30-day metrics,
    which is good enough for a baseline but is labelled as such.
    """
    if getattr(args, "offline", False):
        perf_dir = default_data_dir()
        if not (perf_dir / "performance.csv").exists():
            perf_dir = DATA_DIR
        platform = MockPlatform.from_data(perf_dir)
        return lambda product_id, started_at: baseline_for(
            platform.performance.get(product_id, []), started_at
        )

    def online(product_id: str, started_at) -> dict:
        metrics = getattr(products_by_id.get(product_id), "metrics", None)
        if metrics is None or not getattr(metrics, "pv", 0):
            return {}
        return {
            "days": int(metrics.days),
            "impressions": int(metrics.pv),
            "clicks": int(metrics.uv),
            "orders": int(metrics.orders),
            "returns": 0,
            "order_rate": round(metrics.orders / metrics.pv, 6),
            "click_rate": round(metrics.uv / metrics.pv, 6),
            "return_rate": 0.0,
            "source": "site_metrics",
        }

    return online


def _published_hook(registrar):
    if registrar is None:
        return None

    def hook(proposal: dict, preview) -> str:
        record = registrar.register(proposal, preview)
        return str(record.get("experiment_id") or "")

    return hook


def cmd_publish_run(args: argparse.Namespace) -> int:
    proposals = _load_proposals(args)
    if proposals is None:
        return 1
    site = _site_platform(args)
    if site is None:
        return 1
    try:
        store = site.store()
        products = site.all_products()
    except SiteAPIError as exc:
        print(f"接口错误: {exc.status} {exc.code}: {exc.message}")
        return 1

    products_by_id = {product.product_id: product for product in products}
    review_path = (
        Path(args.review) if getattr(args, "review", None) else default_data_dir() / "review_results.json"
    )
    memory = None if getattr(args, "no_experiment", False) else MemoryStore()
    registrar = None
    if memory is not None:
        registrar = ExperimentRegistrar(
            memory,
            baseline_provider=_baseline_provider(args, products_by_id),
            store_slug=store.slug,
        )
    orchestrator = PublishOrchestrator(
        site,
        approvals=ApprovalStore(DATA_DIR / "approvals.json"),
        store_slug=store.slug,
        lock_dir=DATA_DIR / "locks",
        verdict_path=review_path,
        on_published=_published_hook(registrar),
    )
    if orchestrator.verdicts:
        print(f"合规结论: 读到 {len(orchestrator.verdicts)} 条（{review_path}）")
    rows = []
    results = []
    registered: list[str] = []
    for proposal in proposals[: args.limit]:
        product = products_by_id.get(proposal["product_id"])
        if product is None:
            continue
        try:
            preview = orchestrator.preview(proposal, product)
        except (SiteAPIError, ValueError) as exc:
            rows.append([_clip(proposal["title"], 22), "失败", "—", _clip(str(exc), 30)])
            continue

        approved_by = None
        experiment_id = ""
        warnings: list[str] = []
        if args.approval_token:
            result = orchestrator.publish(
                proposal, product, approval_token=args.approval_token
            )
            status = result.status
            message = result.message
            product_version = result.product_version_id
            variant_version = result.variant_version_id
            approved_by = result.approved_by
            experiment_id = result.experiment_id
            warnings = list(result.warnings)
            if experiment_id:
                registered.append(experiment_id)
            for warning in warnings:
                print(f"[WARN] {proposal['title']}: {warning}")
        else:
            status = "preview"
            message = "dry-run：未写入站点（需 approve issue 生成令牌）"
            product_version = None
            variant_version = None

        label = {
            "preview": "已预览",
            "approval_required": "待审批",
            "invalid_approval": "令牌无效",
            "busy": "发布中",
            "published": "已发布",
            "blocked": "被拦截",
            "failed": "失败",
            "no_change": "无变化",
        }.get(status, status)
        rows.append(
            [
                _clip(proposal["title"], 22),
                label,
                f"{preview.price_change_pct * 100:.1f}%",
                "需要" if preview.requires_approval else "不需要",
                _clip(message, 26),
            ]
        )
        results.append(
            {
                "product_id": proposal["product_id"],
                "variant_key": preview.variant_key,
                "status": status,
                "message": message,
                "price_change_pct": preview.price_change_pct,
                "requires_approval": preview.requires_approval,
                "blocked": preview.blocked,
                "verdict": preview.verdict,
                "review": asdict(preview.review),
                "product_diff": preview.product_diff,
                "variant_diff": preview.variant_diff,
                "product_version_id": product_version,
                "variant_version_id": variant_version,
                "approved_by": approved_by,
                "experiment_id": experiment_id,
                "warnings": warnings,
            }
        )

    _table(["商品", "状态", "价格变动", "审批", "说明"], rows, [24, 8, 8, 6, 28])
    payload = {
        "store": store.slug,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "approval_token_used": bool(args.approval_token),
        "results": results,
    }
    out = Path(args.out) if args.out else default_data_dir() / "publish_results.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"发布结果已写入 {out}")
    if registered:
        print(f"已登记实验 {len(registered)} 条（experiment judge 可在窗口到期后判定）")
    failed = any(
        item["status"] not in ("preview", "published", "no_change") for item in results
    )
    return 1 if failed else 0


def _mock_data_dir() -> Path:
    """Directory holding the simulated store (skus.json + performance.csv).

    Both files are required: loading a half-empty directory would produce an
    empty performance map and quietly judge every experiment as "no data".
    """
    for candidate in (default_data_dir(), DATA_DIR):
        if (candidate / "performance.csv").exists() and (candidate / "skus.json").exists():
            return candidate
    return DATA_DIR


def _task_index(path: Path | None = None) -> dict[str, dict]:
    """product_id -> task order, so a manual registration still carries判据."""
    source = Path(path) if path else default_data_dir() / "task_orders.json"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = payload.get("selected") or payload.get("tasks") or []
    return {
        str(item.get("product_id")): item
        for item in items
        if isinstance(item, dict) and item.get("product_id")
    }


def _local_daily(series_key: str) -> list:
    """Daily rows for one product, from the local simulated store.

    The live site only exposes rolling windows, so a site product has no daily
    series here; ``_site_window_rows`` reconstructs its window instead of
    judging it ``insufficient`` for want of a file.
    """
    return MockPlatform.from_data(_mock_data_dir()).performance.get(series_key, [])


def _site_window_rows(site, record: dict, as_of=None) -> list:
    """Fold a site window's totals into rows the judge can sum.

    The judge only adds rows up; it never reads the day-by-day shape. Spreading
    the window total over the window's days keeps ``sample["days"]`` honest
    without claiming we know the split, which the site does not expose.

    Two rolling reads give the slice: a window that closed ``age`` days ago is
    ``metrics(window + age) - metrics(age)``.
    """
    start = parse_day(record.get("started_at"))
    if start is None or site is None:
        return []
    window = clamp_window(record.get("window_days"))
    today = parse_day(as_of) or date.today()
    end = start + timedelta(days=window)
    age = max(0, (today - end).days)
    total_days = min(90, window + age)
    cut = max(0, total_days - window)
    product_id = str(record.get("product_id") or "")
    if not product_id:
        return []
    try:
        totals = site.window_totals(product_id, days_total=total_days, days_cut=cut)
    except SiteAPIError:
        return []
    impressions = int(totals.get("impressions") or 0)
    orders = int(totals.get("orders") or 0)
    clicks = int(totals.get("clicks") or 0)
    if impressions <= 0 and orders <= 0:
        return []
    rows = []
    for index in range(window):
        first = 1 if index == 0 else 0
        rows.append(
            {
                "day": (start + timedelta(days=index)).isoformat(),
                "impressions": impressions // window + (impressions % window if first else 0),
                "clicks": clicks // window + (clicks % window if first else 0),
                "orders": orders // window + (orders % window if first else 0),
                "returns": 0,
            }
        )
    return rows


def cmd_experiment_list(args: argparse.Namespace) -> int:
    memory = MemoryStore()
    items = memory.read_experiments(limit=args.limit, include_open=True)
    if getattr(args, "status", ""):
        items = [item for item in items if str(item.get("status")) == args.status]
    rows = []
    for item in items:
        outcome = item.get("outcome") or {}
        verdict = str(item.get("verdict") or "")
        rows.append(
            [
                _clip(str(item.get("title") or item.get("product_id") or ""), 22),
                _clip(str(item.get("variant_key") or ""), 18),
                "观察中" if not verdict else str(item.get("verdict_cn") or VERDICT_CN.get(verdict, verdict)),
                f"{(outcome.get('lift') or 0) * 100:+.1f}%" if outcome.get("lift") is not None else "—",
                f"{outcome.get('p_value'):.3f}" if outcome.get("p_value") is not None else "—",
                str(item.get("started_at") or ""),
            ]
        )
    if not items:
        print("还没有实验。发布成功后会自动登记（publish run --approval-token）。")
        return 0
    _table(["商品", "版本", "结论", "提升", "p", "开始"], rows, [24, 20, 8, 8, 7, 11])
    counts = experiment_summary(memory.read_experiments(limit=10000, include_open=True))
    print()
    print(
        f"共 {counts['total']} 条 · 观察中 {counts['status'].get('observing', 0)} · 已判定 "
        f"{counts['status'].get('verdict', 0)} · 成立 {counts['verdicts'].get('supported', 0)} · "
        f"不成立 {counts['verdicts'].get('rejected', 0)} · 不确定 {counts['verdicts'].get('inconclusive', 0)} · "
        f"样本不足 {counts['verdicts'].get('insufficient', 0)}"
    )
    return 0


def cmd_experiment_register(args: argparse.Namespace) -> int:
    """Manual registration for a publish that happened before this ledger existed."""
    memory = MemoryStore()
    series_key = args.series_key or args.product_id
    existing_id = experiment_id_for(args.product_id, args.variant_key, args.started_at)
    # Check before warning: re-running the same registration is normal and must
    # not look like a problem.
    if any(
        str(item.get("experiment_id")) == existing_id
        for item in memory.read_experiments(limit=10000, include_open=True)
    ):
        print(f"已存在，跳过：{existing_id}")
        return 0

    task = _task_index().get(args.product_id, {})
    if not task:
        print(f"[WARN] 任务单里没有 {args.product_id}，这条实验将缺少假设与成功判据")
    daily = _local_daily(series_key)
    if not daily:
        print(f"[WARN] 数据源里没有 {series_key} 的按日序列，判定时会给出样本不足")
    if args.min_lift is not None:
        # A manual registration often has no task order to inherit the target from.
        task = dict(task)
        task["min_lift"] = args.min_lift
    baseline = baseline_for(daily, args.started_at)
    record = build_record(
        product_id=args.product_id,
        variant_id=args.variant_key,
        variant_key=args.variant_key,
        task=task,
        store_slug=args.store,
        started_at=args.started_at,
        window_days=args.window_days,
        baseline=baseline,
        title=str(task.get("title") or ""),
        angle=args.angle,
        run_id=str(task.get("run_id") or ""),
        series_key=series_key,
    )
    created = memory.append_experiment(record)
    if not created:
        print(f"已存在，跳过：{record['experiment_id']}")
        return 0
    print(
        f"已登记 {record['experiment_id']} · 基线 {baseline.get('days', 0)} 天 / "
        f"曝光 {baseline.get('impressions', 0)} / 订单 {baseline.get('orders', 0)} · "
        f"窗口 {record['window_days']} 天"
    )
    return 0


def _judge_due_experiments(
    memory, *, as_of=None, limit: int = 20, rejudge: bool = False, site=None
) -> list[dict]:
    """Judge every experiment whose window has closed.

    Copy only: no model call, so the director can run this at the top of the week
    without spending anything. Lesson writing stays a separate, explicit step.

    A record whose key is missing from the local performance file is a product
    picked from the live site, not a broken record: ``_site_window_rows`` reads
    its window from the site. Without a site connection it falls back to the old
    behaviour (``insufficient`` with a plain reason).
    """
    records = memory.read_experiments(limit=10000, include_open=True)
    due = [
        item
        for item in records
        if (rejudge or not item.get("verdict")) and is_due(item, as_of)
    ]
    if not due:
        return []
    platform = MockPlatform.from_data(_mock_data_dir())
    judged: list[dict] = []
    for record in due[:limit]:
        series_key = str(record.get("series_key") or record.get("product_id"))
        daily = platform.performance.get(series_key, [])
        if not daily and site is not None:
            daily = _site_window_rows(site, record, as_of)
        outcome = judge(record, daily, as_of=as_of)
        updated = apply_judgement(record, outcome)
        memory.update_experiment(
            str(record.get("experiment_id")),
            {
                key: updated[key]
                for key in ("status", "verdict", "verdict_cn", "outcome", "judged_at")
                if key in updated
            },
        )
        judged.append(updated)
    return judged


def cmd_experiment_judge(args: argparse.Namespace) -> int:
    memory = MemoryStore()
    records = memory.read_experiments(limit=10000, include_open=True)
    due = [
        item
        for item in records
        if (args.all or not item.get("verdict")) and is_due(item, args.as_of)
    ]
    if not due:
        print("没有到期的实验。窗口未满的实验会在 experiment list 里显示观察中。")
        return 0

    # Optional: without a token the command still works, it just cannot rebuild
    # windows for products that came from the site.
    try:
        base = getattr(args, "base_url", None) or os.environ.get("SITE_AGENT_BASE_URL")
        site = SitePlatform(base_url=base) if base else SitePlatform()
    except ValueError:
        site = None

    judged = _judge_due_experiments(
        memory, as_of=args.as_of, limit=args.limit, rejudge=args.all, site=site
    )
    rows = []
    for updated in judged:
        detail = updated.get("outcome") or {}
        rows.append(
            [
                _clip(str(updated.get("title") or updated.get("product_id") or ""), 22),
                _clip(str(updated.get("variant_key") or ""), 16),
                str(updated.get("verdict_cn") or updated.get("verdict") or ""),
                f"{(detail.get('lift') or 0) * 100:+.1f}%" if detail.get("lift") is not None else "—",
                str((detail.get("sample") or {}).get("impressions") or 0),
                _clip(str(detail.get("note") or ""), 34),
            ]
        )
    _table(["商品", "版本", "结论", "提升", "曝光", "依据"], rows, [24, 18, 8, 8, 8, 36])

    counts = experiment_summary(memory.read_experiments(limit=10000, include_open=True))
    print()
    print(
        f"判定 {len(judged)} 条 · 成立 {counts['verdicts'].get('supported', 0)} · "
        f"不成立 {counts['verdicts'].get('rejected', 0)} · 不确定 {counts['verdicts'].get('inconclusive', 0)} · "
        f"样本不足 {counts['verdicts'].get('insufficient', 0)}"
    )

    if getattr(args, "no_lessons", False):
        return 0
    finished = [
        item
        for item in memory.read_experiments(limit=200, include_open=False)
        if item.get("verdict")
    ]
    if not finished:
        return 0
    settings = llm_settings()
    planner = _resolve_planner(getattr(args, "planner", "auto"), settings)
    if planner != "llm":
        print("[WARN] 模型未配置，本轮只判定不归纳经验（经验库保持不变）。")
        return 0
    client = LLMClient(settings=settings)
    trace = TraceRecorder()
    try:
        lessons, model, issues = derive_lessons(client, finished, memory.read_lessons(), trace)
    except LessonError as exc:
        print(f"[WARN] 经验库未更新：{exc}")
        return 1
    for issue in issues:
        print(f"[WARN] {issue}")
    if lessons:
        memory.write_lessons(lessons)
        print(f"经验库已更新 {len(lessons)} 条（{model}）")
    trace_summary = trace.summary()
    print(
        f"归纳调用 {trace_summary['calls']} 次 · token {trace_summary['tokens']} · trace {trace_summary['path']}"
    )
    return 0


CONSOLE_DIR = DATA_DIR / "console"
CONSOLE_DB = CONSOLE_DIR / "console.db"
CONSOLE_TENANT_DIR = DATA_DIR / "tenants"
CONSOLE_SEED_FILES = (
    "task_orders.json",
    "workshop_proposals.json",
    "review_results.json",
    "publish_results.json",
    "site_snapshot.json",
)


def _seed_console_tenant(tenant_data: Path) -> None:
    tenant_data.mkdir(parents=True, exist_ok=True)
    for name in CONSOLE_SEED_FILES:
        source = DATA_DIR / name
        target = tenant_data / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)


def cmd_console_init(args: argparse.Namespace) -> int:
    if len(args.admin_password) < 8:
        print("管理员密码至少 8 位")
        return 1
    users_path = CONSOLE_DIR / "users.json"
    tenants_path = CONSOLE_DIR / "tenants.json"
    if users_path.exists() and not args.force:
        print(f"{users_path} 已存在；如需重建请加 --force")
        return 1
    if args.force:
        users_path.unlink(missing_ok=True)
        tenants_path.unlink(missing_ok=True)
        CONSOLE_DB.unlink(missing_ok=True)

    users = UserStore(users_path, users={})
    tenants = TenantStore(tenants_path, DATA_DIR)
    globaltrade_dir = CONSOLE_TENANT_DIR / "globaltrade"
    demo_dir = CONSOLE_TENANT_DIR / "northwind-demo"
    _seed_console_tenant(globaltrade_dir)
    _seed_console_tenant(demo_dir)

    try:
        users.add(args.admin_user, args.admin_password, "admin", ["globaltrade", "northwind-demo"])
        if args.approver_password:
            users.add("approver", args.approver_password, "approver", ["globaltrade"])
        if args.operator_password:
            users.add("operator", args.operator_password, "operator", ["globaltrade"])
    except ValueError as exc:
        print(f"创建用户失败: {exc}")
        return 1

    tenants.add(
        Tenant(
            tenant_id="globaltrade",
            name="GlobalTrade Manufacturing",
            site_base_url=args.site_base_url,
            token_env=args.token_env,
            data_dir=globaltrade_dir,
        )
    )
    tenants.add(
        Tenant(
            tenant_id="northwind-demo",
            name="Northwind Demo（副本）",
            site_base_url="",
            token_env="",
            data_dir=demo_dir,
        )
    )
    db = ConsoleDB(CONSOLE_DB)
    migrate_json(db, users_path, tenants_path, DATA_DIR)
    db.close()
    print(f"已初始化 OpsPilot：{users_path}")
    print(f"SQLite：{CONSOLE_DB}")
    print(f"用户: {', '.join(sorted(users.users))}")
    print(f"租户: {', '.join(item.tenant_id for item in tenants.all())}")
    print("启动: python run.py console serve")
    return 0


def cmd_console_migrate(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else CONSOLE_DB
    db = ConsoleDB(db_path)
    try:
        users_count, tenants_count = migrate_json(
            db, CONSOLE_DIR / "users.json", CONSOLE_DIR / "tenants.json", DATA_DIR
        )
    finally:
        db.close()
    print(f"已迁移到 {db_path}：用户 {users_count}，租户 {tenants_count}")
    return 0


def cmd_console_serve(args: argparse.Namespace) -> int:
    # 运营台是长驻进程，扫描线程里会读 SITE_AGENT_TOKEN。在进程入口把 .env
    # 读进来，否则页面点「扫描店铺」会报「环境变量为空」，而命令行却能跑通——
    # 那种一半能跑一半不能跑的现象最难查。
    load_env()
    db = None
    db_path = Path(args.db) if args.db else (CONSOLE_DB if CONSOLE_DB.exists() else None)
    if db_path is not None:
        if not db_path.exists():
            print(f"数据库不存在: {db_path}")
            return 1
        db = ConsoleDB(db_path)
        users = SQLiteUserStore(db)
        tenants = SQLiteTenantStore(db, DATA_DIR)
        mode = f"sqlite:{db_path}"
    else:
        users = UserStore(CONSOLE_DIR / "users.json")
        tenants = TenantStore(CONSOLE_DIR / "tenants.json", DATA_DIR)
        mode = "json"
    if not users.users:
        print("还没有用户，请先运行: python run.py console init --admin-password ...")
        if db is not None:
            db.close()
        return 1
    audit = AuditDB(CONSOLE_DIR / "audit.db")
    site_factory = None
    if args.demo:
        site_factory = lambda tenant: SnapshotSite(tenant.data_dir / "site_snapshot.json")
    app = ConsoleApp(
        users,
        tenants,
        audit=audit,
        site_factory=site_factory,
        demo=bool(args.demo),
    )
    try:
        server = ConsoleServer(
            app,
            args.host,
            args.port,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
    except (OSError, ValueError) as exc:
        print(f"启动失败: {exc}")
        audit.close()
        if db is not None:
            db.close()
        return 1
    scheme = "https" if args.tls_cert else "http"
    demo_label = " [DEMO 只读]" if args.demo else ""
    print(f"OpsPilot 运营台: {scheme}://{args.host}:{args.port}  [{mode}]{demo_label}")
    print("Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        audit.close()
        if db is not None:
            db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ops-agent", description="跨境商品运营 Agent（第 2 步）")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="生成 50 个 SKU 与 28 天模拟表现数据")
    seed.add_argument("--out", default=None, help="输出目录，默认 data/")
    seed.add_argument("--count", type=int, default=50)
    seed.add_argument("--days", type=int, default=28)
    seed.add_argument("--seed", type=int, default=42)
    seed.set_defaults(func=cmd_seed)

    summary = sub.add_parser("summary", help="按机会分列出优先优化的 SKU")
    summary.add_argument("--data", default=None, help="数据目录，默认 data/")
    summary.add_argument("--top", type=int, default=10)
    summary.set_defaults(func=cmd_summary)

    simulate = sub.add_parser("simulate", help="模拟单个 SKU 改动前后的预期指标")
    simulate.add_argument("--data", default=None)
    simulate.add_argument("--sku", required=True)
    simulate.add_argument("--change", required=True, choices=sorted(
        ["scene_image", "title_keywords", "detail_size_chart", "price_cut", "faster_shipping"]
    ))
    simulate.add_argument("--value", type=float, default=None, help="price_cut 的比例，如 0.08")
    simulate.set_defaults(func=cmd_simulate)

    tools = sub.add_parser("tools", help="工具清单与端到端演示")
    tools_sub = tools.add_subparsers(dest="tools_command", required=True)

    tools_list = tools_sub.add_parser("list", help="列出 9 个工具及其读写/审批属性")
    tools_list.set_defaults(func=cmd_tools_list)

    tools_demo = tools_sub.add_parser("demo", help="跑一条完整链路：读取→生成→合规→审批→发布→回滚")
    tools_demo.add_argument("--sku", required=True)
    tools_demo.add_argument("--data", default=None)
    tools_demo.set_defaults(func=cmd_tools_demo)

    site = sub.add_parser("site", help="连接真实店铺的 Agent API（v1.1）")
    site_sub = site.add_subparsers(dest="site_command", required=True)
    site_parent = argparse.ArgumentParser(add_help=False)
    site_parent.add_argument("--base-url", default=None, help="默认 http://localhost:3000/api/agent")

    site_check = site_sub.add_parser("check", parents=[site_parent], help="连接、店铺、能力清单")
    site_check.set_defaults(func=cmd_site_check)

    site_products = site_sub.add_parser("products", parents=[site_parent], help="读取商品与指标")
    site_products.add_argument("--limit", type=int, default=10)
    site_products.set_defaults(func=cmd_site_products)

    site_sync = site_sub.add_parser("sync", parents=[site_parent], help="把真实店铺快照同步到本地 JSON")
    site_sync.add_argument("--out", default=None)
    site_sync.set_defaults(func=cmd_site_sync)

    site_dry = site_sub.add_parser("dry-run", parents=[site_parent], help="预演改价/改库存，不落库")
    site_dry.add_argument("--variant-id", required=True)
    site_dry.add_argument("--price-cents", type=int, default=None)
    site_dry.add_argument("--stock", type=int, default=None)
    site_dry.set_defaults(func=cmd_site_dry_run)

    intake = sub.add_parser("intake", help="一句话委托：意图识别 → 合约草案预览（只打印）")
    intake_sub = intake.add_subparsers(dest="intake_command", required=True)
    intake_run = intake_sub.add_parser("run", help="识别一句话并给出 task order 草案，不落库")
    intake_run.add_argument("--text", required=True, help="用户原话，例如 \"这20个新品上到美国站\"")
    intake_run.add_argument("--dry-run", action="store_true", help="必填；本步只支持 dry-run")
    intake_run.add_argument(
        "--ingest",
        action="store_true",
        help="走供应商资料链路：CSV → Listing 草稿（JSONL 到 stdout，不落库）",
    )
    intake_run.add_argument(
        "--source",
        default=None,
        help="供应商 CSV 的相对路径（限 data/ingest 下）；不填则取该目录第一个 csv",
    )
    intake_run.add_argument("--snapshot", default=None, help="默认租户快照 data/tenants/globaltrade/site_snapshot.json")
    intake_run.set_defaults(func=cmd_intake_run)

    director = sub.add_parser("director", help="运营主管：体检、选品、诊断、假设、派活（模型决策）")
    director_sub = director.add_subparsers(dest="director_command", required=True)
    director_run = director_sub.add_parser("run", parents=[site_parent], help="跑一遍本周决策")
    director_run.add_argument("--offline", action="store_true", help="用本地快照，不连站点")
    director_run.add_argument("--snapshot", default=None, help="默认租户快照 data/tenants/globaltrade/site_snapshot.json")
    director_run.add_argument("--limit", default="auto", help="auto=模型按店铺规模决定；数字=人工上限")
    director_run.add_argument("--days", type=int, default=30)
    director_run.add_argument("--no-memory", action="store_true", help="本次不读写记忆（演练用）")
    director_run.add_argument("--runtime", choices=["oneshot", "agent"], default=None,
                              help="oneshot=一次判定（默认）；agent=工具循环，模型自己取证据")
    director_run.add_argument("--out", default=None)
    director_run.set_defaults(func=cmd_director_run)

    supervisor = sub.add_parser("supervisor", help="[legacy] 规则基线：阈值选品与诊断")
    supervisor_sub = supervisor.add_subparsers(dest="supervisor_command", required=True)
    supervisor_run = supervisor_sub.add_parser("run", parents=[site_parent], help="跑一遍本周计划")
    supervisor_run.add_argument("--offline", action="store_true", help="用本地快照，不连站点")
    supervisor_run.add_argument("--snapshot", default=None, help="默认租户快照 data/tenants/globaltrade/site_snapshot.json")
    supervisor_run.add_argument("--limit", default="auto", help="auto=规则默认 5 个；数字=指定个数")
    supervisor_run.add_argument("--days", type=int, default=30)
    supervisor_run.add_argument("--planner", choices=["auto", "rule", "llm"], default="rule",
                                help="auto=有模型转 director、失败降级规则；rule=规则基线；llm=必须走模型")
    supervisor_run.add_argument("--runtime", choices=["oneshot", "agent"], default=None,
                                help="转交 director 时的运行时；默认 oneshot")
    supervisor_run.add_argument("--out", default=None)
    supervisor_run.set_defaults(func=cmd_supervisor_run)

    workshop = sub.add_parser("workshop", help="内容工坊：TaskOrder → 2–5 版方案")
    workshop_sub = workshop.add_subparsers(dest="workshop_command", required=True)
    workshop_run = workshop_sub.add_parser("run", parents=[site_parent], help="生成方案包并过合规")
    workshop_run.add_argument("--offline", action="store_true", help="用本地快照，不连站点")
    workshop_run.add_argument("--tasks", default=None, help="默认 data/task_orders.json")
    workshop_run.add_argument("--snapshot", default=None, help="默认租户快照 data/tenants/globaltrade/site_snapshot.json")
    workshop_run.add_argument("--limit", default="auto", help="auto=任务单里全部；数字=只做前几个")
    workshop_run.add_argument("--market", choices=["US", "DE", "UK"], default="US")
    workshop_run.add_argument("--planner", choices=["auto", "rule", "llm"], default="auto",
                              help="auto=有模型就用模型；rule=模板基线；llm=强制模型")
    workshop_run.add_argument("--out", default=None)
    workshop_run.set_defaults(func=cmd_workshop_run)

    agent = sub.add_parser("agent", help="多 Agent 协作：主管 → 工坊（一条命令）")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_run = agent_sub.add_parser("run", parents=[site_parent], help="一条命令跑完决策与方案")
    agent_run.add_argument("--offline", action="store_true", help="用本地快照，不连站点")
    agent_run.add_argument("--snapshot", default=None)
    agent_run.add_argument("--limit", default="auto", help="auto=模型按店铺规模决定；数字=人工上限")
    agent_run.add_argument("--days", type=int, default=30)
    agent_run.add_argument("--market", choices=["US", "DE", "UK"], default="US")
    agent_run.add_argument("--planner", choices=["rule", "llm"], default="llm")
    agent_run.add_argument("--runtime", choices=["oneshot", "agent"], default=None,
                           help="oneshot=一次判定（默认）；agent=工具循环")
    agent_run.add_argument("--tasks-out", default=None)
    agent_run.add_argument("--proposals-out", default=None)
    agent_run.add_argument("--review-out", default=None)
    agent_run.set_defaults(func=cmd_agent_run)

    review = sub.add_parser("review", help="合规审查：逐版检查，可否决")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_run = review_sub.add_parser("run", parents=[site_parent], help="审查方案包")
    review_run.add_argument("--offline", action="store_true", help="用本地快照")
    review_run.add_argument("--proposals", default=None, help="默认 data/workshop_proposals.json")
    review_run.add_argument("--snapshot", default=None, help="默认租户快照 data/tenants/globaltrade/site_snapshot.json")
    review_run.add_argument("--limit", default="auto", help="auto=全部方案；数字=只审前几个")
    review_run.add_argument("--market", choices=["US", "DE", "UK"], default="US")
    review_run.add_argument("--planner", choices=["auto", "rule", "llm"], default="auto",
                            help="auto=有模型就用合规 Agent；rule=词表基线")
    review_run.add_argument("--out", default=None)
    review_run.set_defaults(func=cmd_review_run)

    publish = sub.add_parser("publish", help="发布编排：默认只预览，--approval-token 才写入")
    publish_sub = publish.add_subparsers(dest="publish_command", required=True)
    publish_run = publish_sub.add_parser("run", parents=[site_parent], help="预览或发布方案")
    publish_run.add_argument("--proposals", default=None)
    publish_run.add_argument("--limit", type=int, default=1)
    publish_run.add_argument("--approval-token", default=None, help="approve issue 生成的一次性令牌")
    publish_run.add_argument("--review", default=None, help="默认 data/review_results.json")
    publish_run.add_argument("--no-experiment", action="store_true", help="发布后不登记实验")
    publish_run.add_argument("--out", default=None)
    publish_run.set_defaults(func=cmd_publish_run)

    experiment = sub.add_parser("experiment", help="实验台账：登记、判定、经验回灌")
    experiment_sub = experiment.add_subparsers(dest="experiment_command", required=True)

    experiment_list = experiment_sub.add_parser("list", help="查看实验与结论")
    experiment_list.add_argument("--limit", type=int, default=20)
    experiment_list.add_argument("--status", choices=["observing", "verdict"], default="")
    experiment_list.set_defaults(func=cmd_experiment_list)

    experiment_register = experiment_sub.add_parser("register", help="手工补登记一条实验")
    experiment_register.add_argument("--product-id", required=True)
    experiment_register.add_argument("--variant-key", required=True)
    experiment_register.add_argument("--started-at", default=None, help="默认今天；补登历史实验时传发布日")
    experiment_register.add_argument("--window-days", type=int, default=None)
    experiment_register.add_argument("--angle", default="")
    experiment_register.add_argument("--series-key", default="", help="按日数据的主键，默认同 product-id")
    experiment_register.add_argument("--min-lift", type=float, default=None, help="目标提升，缺省取任务单")
    experiment_register.add_argument("--store", default="")
    experiment_register.set_defaults(func=cmd_experiment_register)

    experiment_judge = experiment_sub.add_parser("judge", help="判定到期实验并回灌经验库")
    experiment_judge.add_argument("--as-of", default=None, help="默认今天；用于回放历史判定")
    experiment_judge.add_argument("--limit", type=int, default=20)
    experiment_judge.add_argument("--all", action="store_true", help="连已判定的也重算")
    experiment_judge.add_argument("--no-lessons", action="store_true", help="只判定，不调用模型归纳")
    experiment_judge.add_argument("--planner", choices=["auto", "llm", "rule"], default="auto")
    experiment_judge.add_argument("--base-url", default=None, help="站点地址；给了才回读站点窗口")
    experiment_judge.set_defaults(func=cmd_experiment_judge)

    approve = sub.add_parser("approve", help="审批令牌：签发 / 查看")
    approve_sub = approve.add_subparsers(dest="approve_command", required=True)
    approve_issue = approve_sub.add_parser("issue", help="签发一次性发布令牌")
    approve_issue.add_argument("--product-id", required=True)
    approve_issue.add_argument("--variant-key", required=True)
    approve_issue.add_argument("--operator", required=True)
    approve_issue.add_argument("--ttl", type=int, default=86400)
    approve_issue.add_argument("--review", default=None, help="默认 data/review_results.json")
    approve_issue.set_defaults(func=cmd_approve_issue)
    approve_list = approve_sub.add_parser("list", help="查看未使用的令牌（脱敏）")
    approve_list.set_defaults(func=cmd_approve_list)

    console = sub.add_parser("console", help="OpsPilot 运营台：初始化 / 启动")
    console_sub = console.add_subparsers(dest="console_command", required=True)
    console_init = console_sub.add_parser("init", help="初始化用户、租户和演示数据")
    console_init.add_argument("--admin-user", default="admin")
    console_init.add_argument("--admin-password", required=True)
    console_init.add_argument("--approver-password", default=None)
    console_init.add_argument("--operator-password", default=None)
    console_init.add_argument("--site-base-url", default="http://localhost:3000/api/agent")
    console_init.add_argument("--token-env", default="SITE_AGENT_TOKEN")
    console_init.add_argument("--force", action="store_true")
    console_init.set_defaults(func=cmd_console_init)
    console_serve = console_sub.add_parser("serve", help="启动运营台")
    console_serve.add_argument("--host", default="127.0.0.1")
    console_serve.add_argument("--port", type=int, default=8787)
    console_serve.add_argument("--db", default=None, help="SQLite 路径；默认 data/console/console.db")
    console_serve.add_argument("--demo", action="store_true", help="离线演示：快照预览，禁止写入")
    console_serve.add_argument("--tls-cert", default=None)
    console_serve.add_argument("--tls-key", default=None)
    console_serve.set_defaults(func=cmd_console_serve)
    console_migrate = console_sub.add_parser("migrate", help="把 users.json / tenants.json 迁到 SQLite")
    console_migrate.add_argument("--db", default=None)
    console_migrate.set_defaults(func=cmd_console_migrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
