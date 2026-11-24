"""供应商资料 → 结构化 → Listing 草稿（Step 7）。

这一步只做"把资料变成能看的草稿"，**不落库、不发布、不调模型**。整条链路的
三个判断都写在代码里，不交给调用方自觉：

1. **缺字段就标 ``needs_fill``，不补数值。**
   成本、售价、类目这些是商业事实，猜一个数字比留空危险得多。留空还能被人
   一眼看见，猜出来的数字会一路漂到前台。

2. **规格必须能在源资料里找到原文，否则 ``verified=False``。**
   复用 ``agents/base.py`` 的红线思路（``spec_numbers``）：草稿里出现的每个
   规格数字都得回到供应商那一行的文本里。找不到就是找不到，不写"约"、
   不写区间、不四舍五入到"好看"的数字。

3. **合规预检不可跳过。**
   ``build_draft`` 在没有传入 checker 时会自己造一个只读的，读词库失败也只会
   退到内置基线（``ComplianceChecker`` 本身的失败关闭语义）。命中即整条
   ``blocked``，草稿仍然返回，但状态说明它不能往下走。

产物是 ``DraftSet``，不是 ``Proposal``：工坊的 ``Proposal`` 强制 2—5 个变体，
上新草稿在拿到真实卖点之前没有"变体"可分，套进去只会被校验卡住。
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .agents.base import spec_numbers
from .compliance import ComplianceChecker
from .config import DATA_DIR
from .models import Market

# ---------------------------------------------------------------------------
# 边界常量
# ---------------------------------------------------------------------------

# 供应商表必须有这五列，少一列这条就进不了执行。
REQUIRED_FIELDS: tuple[str, ...] = ("sku", "title", "category", "cost", "price")

# 规格类字段：允许缺失，但缺失或对不上原文时必须留痕。
SPEC_FIELDS: tuple[str, ...] = ("dimensions", "weight", "material")

# 其余可识别列。认不出的列不报错，只在 notes 里点名，避免供应商模板一改就炸。
OPTIONAL_FIELDS: tuple[str, ...] = ("market", "language", "currency", "image_brief")

# 一次最多处理多少行。真实上新的批次按 SKU 拆，几百行就该走分批而不是一次灌。
MAX_ROWS = 200
MAX_BYTES = 512 * 1024

MAX_TITLE_CHARS = 120
MAX_BULLET_CHARS = 200
MAX_DESCRIPTION_CHARS = 1200
MAX_FACTS = 20
MAX_FACT_CHARS = 200

STATUS_READY = "ready"
STATUS_NEEDS_FILL = "needs_fill"
STATUS_BLOCKED = "blocked"
STATUSES: tuple[str, ...] = (STATUS_READY, STATUS_NEEDS_FILL, STATUS_BLOCKED)


class IngestError(ValueError):
    """供应商资料读不了 / 路径不合规。带 code 方便 CLI 与测试分别断言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 供应商 CSV
# ---------------------------------------------------------------------------


@dataclass
class ParsedCsv:
    """一次解析的结果。``rows`` 里的键一律小写、去空白。"""

    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def resolve_ingest_path(raw, root: Path | str | None = None) -> tuple[Path, str]:
    """把用户给的路径收敛到 ``<root>/...csv``，越界就拒绝。

    先拒 ``..`` 再 ``resolve()``，最后再做一次 ``relative_to`` 兜底——顺序不能反：
    只做后者的话，符号链接指到外面也能通过；只做前者的话，Windows 的短路径和
    盘符写法会漏。两道都做，才挡得住 ``../../etc/passwd`` 这类输入。
    """

    base = Path(root) if root is not None else DATA_DIR / "ingest"
    base = base.resolve()
    text = str(raw or "").strip()
    if not text:
        raise IngestError("empty_path", "path 不能为空")

    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive or text[:1] in ("/", "\\"):
        raise IngestError("absolute_path", f"只接受 {base.name}/ 下的相对路径：{text}")
    if any(part == ".." for part in candidate.parts):
        raise IngestError("path_traversal", f"路径不允许包含 ..：{text}")
    if candidate.suffix.lower() != ".csv":
        raise IngestError("bad_extension", f"只支持 .csv：{text}")

    target = (base / candidate).resolve()
    try:
        relative = target.relative_to(base)
    except ValueError:
        raise IngestError("path_traversal", f"路径越界：{text} 不在 {base} 下") from None
    return target, relative.as_posix()


def parse_supplier_csv(raw, root: Path | str | None = None) -> ParsedCsv:
    """校验路径 → 读文件。路径策略与读取分开，读的那半可以单独测。"""

    target, _ = resolve_ingest_path(raw, root)
    return read_supplier_csv(target)


def _header(name: str) -> str:
    """表头归一：去空白、转小写、连字符与空格统一成下划线。"""

    text = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    return re.sub(r"[\s\-]+", "_", text)


# 表头认得的列。认不出的列不报错，只在 notes 里点名。
_KNOWN_COLUMNS: frozenset[str] = frozenset(
    {_header(name) for name in (*REQUIRED_FIELDS, *SPEC_FIELDS, *OPTIONAL_FIELDS)}
)


def read_supplier_csv(path: Path | str) -> ParsedCsv:
    """读一个已经确认过位置的 CSV。这一层不做任何路径判断。"""

    target = Path(path)
    if not target.exists():
        raise IngestError("file_not_found", f"找不到供应商资料：{target}")
    size = target.stat().st_size
    if size > MAX_BYTES:
        raise IngestError("file_too_large", f"文件 {size} 字节，超过上限 {MAX_BYTES}")

    try:
        # utf-8-sig：Excel 导出的 CSV 常带 BOM，不去掉的话第一列名会变成
        # "\ufeffsku"，五列全缺，整批判成 needs_fill。
        with target.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            raw_columns = reader.fieldnames or []
            mapping = {_header(name): name for name in raw_columns if _header(name)}
            columns = sorted(mapping)
            ignored = [name for name in raw_columns if _header(name) not in _KNOWN_COLUMNS]
            result = ParsedCsv(columns=columns, ignored_columns=ignored)
            if not raw_columns:
                raise IngestError("empty_csv", f"{target} 没有表头")
            missing_columns = [name for name in REQUIRED_FIELDS if name not in mapping]
            if missing_columns:
                result.notes.append(f"表头缺少 {'、'.join(missing_columns)}，这些字段会按缺失处理")
            if ignored:
                result.notes.append(f"忽略未识别列：{'、'.join(ignored[:5])}")

            for raw_row in reader:
                row = {
                    _header(name): str(value or "").strip()
                    for name, value in raw_row.items()
                    if name is not None
                }
                if not any(row.values()):
                    continue  # 空行不是一条草稿
                row["_line"] = str(reader.line_num)
                result.rows.append(row)
                if len(result.rows) >= MAX_ROWS:
                    result.notes.append(f"单次最多 {MAX_ROWS} 行，其余未读（请分批）")
                    break
    except UnicodeDecodeError as exc:
        raise IngestError("bad_encoding", f"文件不是 UTF-8：{exc}") from exc
    except OSError as exc:
        raise IngestError("unreadable", f"读不了 {target}：{exc}") from exc

    if not result.rows:
        result.notes.append("文件里没有数据行")
    return result


# ---------------------------------------------------------------------------
# 草稿
# ---------------------------------------------------------------------------


@dataclass
class DraftSet:
    """一条上新草稿。形状比 ``Proposal`` 简单：没有变体，只有事实与状态。

    ``status`` 的优先级是 blocked > needs_fill > ready：合规否决比缺字段更硬，
    缺字段又比"能出草稿"更硬。``ready`` 不代表能发布，只代表"资料齐了、能进下一步"。
    """

    sku: str
    status: str
    market: str
    language: str
    title: str = ""
    category: str = ""
    bullets: list[str] = field(default_factory=list)
    description: str = ""
    image_brief: str = ""
    price_cents: int = 0
    cost_cents: int = 0
    currency: str = "USD"
    specs: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    assumed: dict = field(default_factory=dict)
    compliance: dict = field(default_factory=dict)
    facts_used: list[str] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)
    source_row: int = 0
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"非法草稿状态：{self.status!r}（只支持 {', '.join(STATUSES)}）")
        if not str(self.sku or "").strip():
            raise ValueError("草稿必须有 sku")

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    @property
    def blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    def to_dict(self) -> dict:
        return {
            "draft_id": f"DRAFT:{self.sku}",
            "source": "supplier_csv",
            "status": self.status,
            "sku": self.sku,
            "market": self.market,
            "language": self.language,
            "title": self.title,
            "category": self.category,
            "bullets": list(self.bullets),
            "description": self.description,
            "image_brief": self.image_brief,
            "price_cents": self.price_cents,
            "cost_cents": self.cost_cents,
            "currency": self.currency,
            "specs": {name: dict(item) for name, item in self.specs.items()},
            "missing": list(self.missing),
            "flags": list(self.flags),
            "assumed": dict(self.assumed),
            "compliance": dict(self.compliance),
            "facts_used": list(self.facts_used),
            "unverified_numbers": list(self.unverified_numbers),
            "source_row": self.source_row,
            "notes": list(self.notes),
            # 本步的硬约束：草稿只是草稿。
            "writes": 0,
        }


def build_drafts(
    rows: list[dict],
    *,
    market=None,
    language: str | None = None,
    facts: list | None = None,
    checker: ComplianceChecker | None = None,
) -> list[DraftSet]:
    """整批构建；重复 sku 只留痕，不静默去重（静默去重会让人以为只进了 3 条）。"""

    seen: dict[str, int] = {}
    drafts: list[DraftSet] = []
    for row in rows:
        sku = str(row.get("sku") or "").strip()
        key = sku.casefold()
        duplicate_of = seen.get(key) if key else None
        if key:
            seen.setdefault(key, len(drafts) + 1)
        drafts.append(
            build_draft(
                row,
                market=market,
                language=language,
                facts=facts,
                checker=checker,
                duplicate_of=duplicate_of,
            )
        )
    return drafts


def build_draft(
    row: dict,
    *,
    market=None,
    language: str | None = None,
    facts: list | None = None,
    checker: ComplianceChecker | None = None,
    duplicate_of: int | None = None,
) -> DraftSet:
    """一行供应商资料 → 一条草稿。异常只在路径/IO 层抛，数据问题都落成状态。"""

    values = {name: _clean(row.get(name)) for name in (*REQUIRED_FIELDS, *SPEC_FIELDS, *OPTIONAL_FIELDS)}
    missing: list[str] = []
    flags: list[str] = []
    assumed: dict[str, bool] = {}
    notes: list[str] = []

    # 连 sku 都没有的行不该炸掉整批：用行号占位，标成缺失，让人回头补资料。
    # 占位符明确写着 ROW-，不会被误当成真实 SKU 往下游走。
    sku = values["sku"]
    if not sku:
        sku = f"ROW-{_line_number(row)}"
        flags.append("missing_sku")
        notes.append("缺少 sku，用行号占位")

    for name in REQUIRED_FIELDS:
        if not values.get(name):
            missing.append(name)

    cost_cents = _money_cents(values.get("cost"))
    price_cents = _money_cents(values.get("price"))
    for name, cents in (("cost", cost_cents), ("price", price_cents)):
        if cents is None and name not in missing:
            # 有值但读不出金额：这是格式问题，不是"没填"，得说清楚。
            missing.append(name)
            notes.append(f"{name} 不是可识别金额：{values.get(name)!r}")

    resolved_market, market_note = _resolve_market(values.get("market"), market)
    requested_market = _as_market(market) if market else None
    if values.get("market"):
        if market_note:
            # 资料里写了市场但认不出：这是格式问题，不能悄悄换成默认市场。
            missing.append("market")
            flags.append("bad_market")
            notes.append(market_note)
        elif requested_market and resolved_market != requested_market:
            # 资料写 US、委托说上德国站：两边都有依据，替哪边决定都是猜。
            # 以前这里静默取资料值，等于把"上错站点"的风险留到发布那一刻。
            missing.append("market")
            flags.append("market_conflict")
            notes.append(
                f"资料市场 {resolved_market.value} 与委托市场 {requested_market.value} 不一致，需人工确认"
            )
    else:
        assumed["market"] = True
        notes.append(f"未指定市场，按店铺默认 {requested_market.value if requested_market else resolved_market.value} 预演")

    resolved_language = values.get("language") or str(language or "en").strip() or "en"
    if not values.get("language"):
        assumed["language"] = True

    row_text = " ".join(value for value in values.values() if value)
    specs, unverified = _build_specs(values, row_text)

    title = values["title"][:MAX_TITLE_CHARS]
    bullets = _bullets(values, specs)
    description = _description(title, values, specs)
    image_brief = values.get("image_brief") or (
        f"{title} on a plain white background, no visible logos or trademarks, no faces"
    )[:MAX_BULLET_CHARS]

    if len(bullets) < 2:
        flags.append("thin_copy")
        notes.append("可陈述的事实不足 2 条，卖点文案留给工坊，不由本步编造")

    if unverified:
        flags.append("spec_unverified")

    compliance = _compliance(bullets, description, image_brief, values, resolved_market)

    leaked = unverified_numbers(
        " ".join([title, *bullets, description, image_brief]),
        row_text,
    )
    if leaked:
        # 走到这里说明拼装逻辑把源资料里没有的数字写进了文案，属于代码缺陷，
        # 不能当成"小问题"放过。
        flags.append("unverified_numbers")
        notes.append(f"文案含源资料外的数字：{sorted(leaked)}")

    if duplicate_of is not None:
        flags.append("duplicate_sku")
        notes.append(f"sku 与前第 {duplicate_of} 条重复")

    status = _status(compliance, missing)

    if status == STATUS_NEEDS_FILL:
        notes.append(f"缺 {'、'.join(missing)}，字段留空，不补数值")
    if status == STATUS_BLOCKED:
        notes.append("合规预检未通过，整条不得进入工坊")

    return DraftSet(
        sku=sku,
        status=status,
        market=resolved_market.value,
        language=resolved_language,
        title=title,
        category=values["category"],
        bullets=bullets,
        description=description[:MAX_DESCRIPTION_CHARS],
        image_brief=image_brief,
        price_cents=price_cents or 0,
        cost_cents=cost_cents or 0,
        currency=(values.get("currency") or "USD").upper(),
        specs=specs,
        missing=missing,
        flags=_dedupe(flags),
        assumed=assumed,
        compliance=compliance,
        facts_used=_facts(facts),
        unverified_numbers=sorted(leaked),
        source_row=_line_number(row),
        notes=notes,
    )


def summarize(drafts: list[DraftSet]) -> dict:
    """按状态计数，给 CLI 的一行摘要用。"""

    counts = {name: 0 for name in STATUSES}
    for draft in drafts:
        counts[draft.status] = counts.get(draft.status, 0) + 1
    flagged = sorted({flag for draft in drafts for flag in draft.flags})
    return {"total": len(drafts), "status": counts, "flags": flagged}


# ---------------------------------------------------------------------------
# 规格溯源
# ---------------------------------------------------------------------------


def verify_spec(value, source_text: str) -> tuple[bool, str]:
    """草稿里的规格能不能在源资料里找到原文。

    带数字的（尺寸/重量）：**每个数字**都得在源文本里出现过，单位也算在内；
    不带数字的（材质等）：整段原文得在源文本里出现。找不到就给原因，不猜。
    """

    text = _clean(value)
    if not text:
        return False, "源资料未提供"
    numbers = spec_numbers(text)
    if numbers:
        missing = numbers - spec_numbers(source_text)
        if missing:
            return False, f"数字 {sorted(missing)} 在源资料里找不到原文"
        return True, ""
    if _normalize(text) and _normalize(text) in _normalize(source_text):
        return True, ""
    return False, "源资料里没有这段原文"


def unverified_numbers(text: str, source_text: str) -> set[str]:
    """草稿文案里出现、但源资料里没有的数字。空集合才叫干净。"""

    return spec_numbers(text) - spec_numbers(source_text)


def _build_specs(values: dict, row_text: str) -> tuple[dict, list[str]]:
    specs: dict = {}
    unverified: list[str] = []
    for name in SPEC_FIELDS:
        raw = values.get(name)
        verified, reason = verify_spec(raw, row_text)
        specs[name] = {
            "value": raw or None,
            "verified": verified,
            "reason": reason,
            "source_text": raw,
        }
        if not verified:
            unverified.append(name)
    return specs, unverified


# ---------------------------------------------------------------------------
# 文案拼装（只陈述事实，不做卖点）
# ---------------------------------------------------------------------------


def _bullets(values: dict, specs: dict) -> list[str]:
    """只写能在源资料里指到原文的事实。说服力不是这一步的目标。"""

    bullets: list[str] = []
    for name, label in (("material", "Material"), ("dimensions", "Dimensions"), ("weight", "Weight")):
        item = specs.get(name) or {}
        if item.get("verified") and item.get("value"):
            bullets.append(f"{label}: {item['value']}"[:MAX_BULLET_CHARS])
    if values.get("category"):
        bullets.append(f"Category: {values['category']}"[:MAX_BULLET_CHARS])
    return bullets[:4]


def _description(title: str, values: dict, specs: dict) -> str:
    parts = [f"{title}."] if title else []
    for name, label in (("material", "Material"), ("dimensions", "Dimensions"), ("weight", "Weight")):
        item = specs.get(name) or {}
        if item.get("verified") and item.get("value"):
            parts.append(f"{label}: {item['value']}.")
    if values.get("category"):
        parts.append(f"Category: {values['category']}.")
    return " ".join(parts)[:MAX_DESCRIPTION_CHARS]


def _compliance(
    bullets: list[str],
    description: str,
    image_brief: str,
    values: dict,
    market: Market,
) -> dict:
    """只读预检。checker 缺失时自建一个——合规不允许"这次先跳过"。"""

    checker = _default_checker()
    text = " ".join([values.get("title") or "", *bullets, description])
    report = checker.check(text, image_brief, values.get("category") or "", market.value)
    payload = report.to_dict()
    payload["checked"] = True
    return payload


_CHECKER: ComplianceChecker | None = None


def _default_checker() -> ComplianceChecker:
    global _CHECKER
    if _CHECKER is None:
        _CHECKER = ComplianceChecker()
    return _CHECKER


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _status(compliance: dict, missing: list[str]) -> str:
    if not compliance.get("passed", False):
        return STATUS_BLOCKED
    if missing:
        return STATUS_NEEDS_FILL
    return STATUS_READY


def _resolve_market(raw, fallback) -> tuple[Market, str]:
    """行内有值就认行内的；认不出就报错让人改资料，不要悄悄换成默认市场。"""

    if raw:
        try:
            return Market(str(raw).strip().upper()), ""
        except ValueError:
            return _as_market(fallback), f"market 非法：{raw!r}（只支持 US/DE/UK）"
    return _as_market(fallback), ""


def _as_market(value) -> Market:
    if isinstance(value, Market):
        return value
    try:
        return Market(str(value or "").strip().upper())
    except ValueError:
        return Market.US


def _money_cents(raw) -> int | None:
    """金额 → 分。认不出、非正数都返回 None，由调用方标缺失。"""

    text = str(raw or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0:
        return None
    return int(round(value * 100))


def _clean(value) -> str:
    return str(value or "").strip()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or "")).casefold()).strip()


def _facts(raw) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    items: list[str] = []
    for value in raw:
        text = _clean(value)[:MAX_FACT_CHARS]
        if text and text not in items:
            items.append(text)
        if len(items) >= MAX_FACTS:
            break
    return items


def _line_number(row: dict) -> int:
    try:
        return int(row.get("_line") or 0)
    except (TypeError, ValueError):
        return 0


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
