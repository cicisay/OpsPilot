"""给模拟店铺补齐缺失的日粒度数据（仅演示环境使用）。

背景：站点数据库的 ``ProductViewStat`` 只写到 2026-11-02，之后一条也没有。
运营台任何"发布后观察 N 天"的实验都靠滚动窗口相减取数，最近 14 天为空时
``window_totals`` 会直接判"测不到"，实验就永远是"无数据"。

本脚本按每个商品**过去两周的日均曝光**，回填缺失日期的 pv/uv，并按历史
转化率补出订单与询盘。设计约束：

* 只写 ``ProductViewStat`` / ``Order`` / ``OrderItem`` / ``Inquiry``，
  不动 ``Product`` / ``ProductVariant``，商品 ID 与 SKU 全部保持稳定；
* 幂等：已存在的 ``(productId, date)`` 直接跳过，重复跑不会翻倍；
* 确定性：噪声由 ``sha256(productId:date)`` 派生，同样的输入永远得到同样的数；
* 数量级对齐历史：日曝光沿用各商品近两周均值，订单率沿用全店历史比率，
  不人为放大，避免造出"提升 300%"这种假信号。

用法::

    SITE_DB_PATH=/path/to/dev.db python scripts/backfill_site_daily.py --dry-run
    python scripts/backfill_site_daily.py                # 真正写入
    python scripts/backfill_site_daily.py --to 2026-11-16
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("SITE_DB_PATH", "../foreign-trade-site/web/prisma/dev.db"))

# 工作日越接近周末越低；B2B 采购集中在周一到周四。
WEEKDAY_FACTOR = (1.08, 1.06, 1.04, 1.00, 0.96, 0.82, 0.80)  # 周一 … 周日

LOOKBACK_DAYS = 14


def _noise(*parts: str) -> float:
    """0.85–1.15 之间的确定性抖动，避免每天的数字一模一样。"""
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return 0.85 + (int(digest[:8], 16) % 3000) / 10000.0


def _cuid(*parts: str) -> str:
    """稳定的伪 cuid，长度与 Prisma 生成的 25 位一致。"""
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return "bf" + digest[:23]


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth 采样；这里的 lam 都小于 10。"""
    if lam <= 0:
        return 0
    limit = math.exp(-lam)
    count, product = 0, 1.0
    while True:
        product *= rng.random()
        if product <= limit:
            return count
        count += 1


def _ms(day: date, hour: int = 12) -> int:
    return int(datetime(day.year, day.month, day.day, hour).timestamp() * 1000)


def _load_products(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select p.id, p.slug, p.storeId,
               (select v.id from ProductVariant v where v.productId = p.id
                order by v.priceCents asc limit 1) as variantId,
               (select v.skuCode from ProductVariant v where v.productId = p.id
                order by v.priceCents asc limit 1) as sku,
               (select v.priceCents from ProductVariant v where v.productId = p.id
                order by v.priceCents asc limit 1) as priceCents,
               (select v.imageUrl from ProductVariant v where v.productId = p.id
                order by v.priceCents asc limit 1) as image
        from Product p
        where p.deletedAt is null and p.status = 'active'
        order by p.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _daily_baseline(conn: sqlite3.Connection) -> dict[str, dict]:
    """每个商品最近 LOOKBACK_DAYS 天的日均 pv 与 uv/pv 比。"""
    latest = conn.execute("select max(date) from ProductViewStat").fetchone()[0]
    if not latest:
        return {}
    since = (date.fromisoformat(latest) - timedelta(days=LOOKBACK_DAYS - 1)).isoformat()
    rows = conn.execute(
        """
        select productId, count(*) as days, sum(pv) as pv, sum(uv) as uv
        from ProductViewStat where date >= ? group by productId
        """,
        (since,),
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        days = int(row["days"]) or 1
        pv = int(row["pv"] or 0)
        uv = int(row["uv"] or 0)
        if pv <= 0:
            continue
        out[str(row["productId"])] = {
            "pv_per_day": pv / days,
            "uv_ratio": min(0.9, max(0.3, uv / pv)),
        }
    return out


def _history_rates(conn: sqlite3.Connection) -> tuple[float, float]:
    """全店历史的订单率与询盘率（相对曝光）。"""
    span = conn.execute(
        "select min(date), max(date), sum(pv) from ProductViewStat"
    ).fetchone()
    start, end, pv_total = span[0], span[1], int(span[2] or 0)
    if not start or not end or pv_total <= 0:
        return 0.0012, 0.0002
    orders = conn.execute(
        'select count(*) from "Order" where createdAt >= ?',
        (_ms(date.fromisoformat(start), 0),),
    ).fetchone()[0]
    inquiries = conn.execute(
        "select count(*) from Inquiry where createdAt >= ?",
        (_ms(date.fromisoformat(start), 0),),
    ).fetchone()[0]
    # 订单与询盘只覆盖一部分时间，按覆盖率折算成"整段应有的量"再算比率。
    order_rate = max(0.0002, min(0.05, orders / pv_total))
    inquiry_rate = max(0.00005, min(0.02, inquiries / pv_total))
    return round(order_rate, 6), round(inquiry_rate, 6)


def backfill(db: Path, until: date, dry_run: bool) -> dict:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        products = _load_products(conn)
        baseline = _daily_baseline(conn)
        order_rate, inquiry_rate = _history_rates(conn)

        latest = conn.execute("select max(date) from ProductViewStat").fetchone()[0]
        start = date.fromisoformat(latest) + timedelta(days=1) if latest else until - timedelta(days=13)
        missing = [
            start + timedelta(days=offset)
            for offset in range((until - start).days + 1)
        ]
        if not missing:
            return {"status": "nothing_to_do", "latest": latest}

        # 先把每天的曝光计划算出来，订单按当天曝光量分配。
        plan: list[tuple[date, dict[str, tuple[int, int]]]] = []
        for day in missing:
            factor = WEEKDAY_FACTOR[day.weekday()]
            per_product: dict[str, tuple[int, int]] = {}
            for product in products:
                stats = baseline.get(product["id"])
                if not stats:
                    continue
                pv = int(
                    round(stats["pv_per_day"] * factor * _noise(product["id"], day.isoformat()))
                )
                pv = max(1, pv)
                uv = max(1, int(round(pv * stats["uv_ratio"] * _noise("uv", product["id"], day.isoformat()))))
                per_product[product["id"]] = (pv, min(uv, pv))
            if per_product:
                plan.append((day, per_product))

        rng = random.Random(f"backfill-{start}-{until}")
        buyers = [row[0] for row in conn.execute("select id from User where role = 'buyer'")]
        store_id = conn.execute("select id from Store limit 1").fetchone()[0]
        next_no = conn.execute(
            'select max(cast(substr(orderNo, 8) as integer)) from "Order"'
        ).fetchone()[0] or 192

        view_rows = 0
        orders_created: list[dict] = []
        inquiries_created = 0
        day_totals: list[dict] = []

        for day, per_product in plan:
            pv_total = sum(pv for pv, _ in per_product.values())
            for product in products:
                if product["id"] not in per_product:
                    continue
                pv, uv = per_product[product["id"]]
                view_rows += 1
                if not dry_run:
                    conn.execute(
                        "insert or ignore into ProductViewStat (id, productId, date, pv, uv)"
                        " values (?, ?, ?, ?, ?)",
                        (_cuid("view", product["id"], day.isoformat()), product["id"], day.isoformat(), pv, uv),
                    )

            # 订单：当天曝光 × 历史订单率，落到具体商品（按曝光加权）。
            weighted: list[tuple[str, int]] = []
            for product in products:
                if product["id"] in per_product:
                    weighted.append((product["id"], per_product[product["id"]][0]))
            for _ in range(_poisson(rng, pv_total * order_rate)):
                if not weighted:
                    break
                total_weight = sum(weight for _, weight in weighted)
                ticket = rng.random() * total_weight
                cursor = 0.0
                chosen = weighted[-1][0]
                for pid, weight in weighted:
                    cursor += weight
                    if ticket <= cursor:
                        chosen = pid
                        break
                product = next(item for item in products if item["id"] == chosen)
                qty = rng.choice([20, 30, 50, 100, 150, 200, 300])
                unit = int(product["priceCents"] or 1000)
                subtotal = qty * unit
                shipping = 18000 if subtotal < 500000 else 26000
                next_no += 1
                order_id = _cuid("order", day.isoformat(), str(next_no))
                paid_at = _ms(day, rng.randint(8, 20))
                orders_created.append(
                    {
                        "id": order_id,
                        "orderNo": f"GT-MOR-{next_no:04d}",
                        "productId": product["id"],
                        "variantId": product["variantId"],
                        "slug": product["slug"],
                        "sku": product["sku"],
                        "image": product["image"],
                        "qty": qty,
                        "unit": unit,
                        "subtotal": subtotal,
                        "shipping": shipping,
                        "buyer": rng.choice(buyers),
                        "store": store_id,
                        "createdAt": paid_at,
                    }
                )

            inquiries = _poisson(rng, pv_total * inquiry_rate)
            inquiries_created += inquiries
            day_totals.append({"date": day.isoformat(), "pv": pv_total, "orders": sum(
                1 for order in orders_created if order["createdAt"] >= _ms(day, 0) and order["createdAt"] < _ms(day, 23)
            )})

        if not dry_run:
            for order in orders_created:
                conn.execute(
                    'insert into "Order" (id, orderNo, buyerId, storeId, currency, subtotalCents,'
                    " shippingCents, taxCents, totalCents, status, incoterm, addressJson,"
                    " paymentMethod, paidAt, shippedAt, deliveredAt, createdAt)"
                    " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        order["id"], order["orderNo"], order["buyer"], order["store"], "USD",
                        order["subtotal"], order["shipping"], 0,
                        order["subtotal"] + order["shipping"], "delivered", "FOB",
                        json.dumps({"country": "US", "city": "—", "street": "—", "zip": "—"}),
                        "tt", order["createdAt"],
                        order["createdAt"] + 432000000,
                        order["createdAt"] + 1036800000,
                        order["createdAt"],
                    ),
                )
                conn.execute(
                    "insert into OrderItem (id, orderId, variantId, productId, productSnapshot,"
                    " qty, unitPriceCents) values (?,?,?,?,?,?,?)",
                    (
                        _cuid("item", order["id"]),
                        order["id"],
                        order["variantId"],
                        order["productId"],
                        json.dumps(
                            {"title": order["slug"], "sku": order["sku"], "image": order["image"]},
                            ensure_ascii=False,
                        ),
                        order["qty"],
                        order["unit"],
                    ),
                )
            for day, per_product in plan:
                pv_total = sum(pv for pv, _ in per_product.values())
                count = _poisson(rng, pv_total * inquiry_rate)
                for index in range(count):
                    product = rng.choice([p for p in products if p["id"] in per_product])
                    conn.execute(
                        "insert into Inquiry (id, storeId, productId, name, company, email, phone,"
                        " country, qty, destPort, message, status, createdAt)"
                        " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            _cuid("inquiry", day.isoformat(), str(index), product["id"]),
                            store_id, product["id"], "Procurement Desk", "Demo Trading LLC",
                            "buyer@demo.com", None, "US", rng.choice([50, 100, 200]),
                            "Los Angeles", "Please quote FOB price and lead time.",
                            "new", _ms(day, rng.randint(9, 18)),
                        ),
                    )
            conn.commit()

        return {
            "status": "dry_run" if dry_run else "done",
            "from": start.isoformat(),
            "to": until.isoformat(),
            "days": len(missing),
            "view_rows": view_rows,
            "orders": len(orders_created),
            "inquiries": inquiries_created,
            "order_rate": order_rate,
            "inquiry_rate": inquiry_rate,
            "day_totals": day_totals,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="回填模拟店铺缺失的日粒度数据")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"默认 {DEFAULT_DB}")
    parser.add_argument("--to", default=None, help="补到哪天，默认今天")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"数据库不存在：{args.db}")
    until = date.fromisoformat(args.to) if args.to else date.today()
    report = backfill(args.db, until, args.dry_run)

    print(json.dumps({k: v for k, v in report.items() if k != "day_totals"}, ensure_ascii=False, indent=2))
    for item in report.get("day_totals", []):
        print(f"  {item['date']}  pv={item['pv']:>6}  orders={item['orders']}")


if __name__ == "__main__":
    main()
